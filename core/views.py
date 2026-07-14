import hashlib
import json
import logging
import random
import smtplib
from datetime import timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from django.conf import settings
from django.core.cache import cache
from django.core.mail import send_mail
from django.db import DatabaseError, transaction
from django.db.models import Avg, Count, Prefetch, Q, Sum
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.utils.decorators import method_decorator
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.views.decorators.csrf import csrf_exempt
from rest_framework import generics, parsers, permissions, status, viewsets
from rest_framework.authtoken.models import Token
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Delivery, Design, DriverProfile, Fabric, MeasurementProfile, Order, PasswordResetOTP, TailorProfile, User, UserSession
from .permissions import IsCustomer, IsDriver, IsTailor
from .serializers import (
    AdminAssignDriverSerializer,
    AdminDriverAssignmentSerializer,
    AdminDriverDetailSerializer,
    DashboardDesignSerializer,
    DashboardFabricSerializer,
    AdminOrderListSerializer,
    AdminOrderDetailSerializer,
    AdminTailorDetailSerializer,
    DashboardSerializer,
    DeliverySerializer,
    DesignSerializer,
    DriverProfileSerializer,
    DriverProfileUpdateSerializer,
    FabricSerializer,
    InvoiceSerializer,
    LoginSerializer,
    MeasurementSerializer,
    NotificationSerializer,
    OrderSerializer,
    PasswordResetConfirmSerializer,
    PasswordResetRequestSerializer,
    PublicTailorSerializer,
    SignupSerializer,
    TailorShopCatalogSerializer,
    TailorShopSetupSerializer,
    TailorOrderDetailSerializer,
    TailorOrderListSerializer,
    TailorProfileSerializer,
    UserSerializer,
    build_auth_payload,
    optimize_inline_image,
    sync_cloudinary_images_or_raise,
    sync_uploaded_files_or_raise,
)

CACHE_VERSION_KEY = 'api-cache-version'
PUBLIC_CACHE_TTL = 120
USER_CACHE_TTL = 60
MAX_CACHEABLE_PAYLOAD_BYTES = 262144
IMAGE_UPLOAD_PARSER_CLASSES = [parsers.JSONParser, parsers.FormParser, parsers.MultiPartParser]
logger = logging.getLogger(__name__)
ONLINE_PAYMENT_METHODS = {Order.PaymentMethod.CARD, Order.PaymentMethod.WALLET}


def get_stripe_module():
    try:
        import stripe
    except ImportError as exc:
        raise RuntimeError('Stripe dependency is not installed on the backend.') from exc

    secret_key = getattr(settings, 'STRIPE_SECRET_KEY', '')
    if not secret_key:
        raise RuntimeError('STRIPE_SECRET_KEY is not configured.')

    stripe.api_key = secret_key
    return stripe


def quantize_money(value):
    try:
        return Decimal(str(value or '0')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError):
        return Decimal('0.00')


def stripe_amount_from_decimal(value):
    amount = quantize_money(value)
    return int((amount * Decimal('100')).quantize(Decimal('1'), rounding=ROUND_HALF_UP))


def calculate_platform_fee(total):
    fee_percent = quantize_money(getattr(settings, 'PLATFORM_FEE_PERCENT', '5'))
    return (quantize_money(total) * fee_percent / Decimal('100')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def apply_paid_state(order, *, payment_intent_id='', checkout_session_id=''):
    platform_fee = calculate_platform_fee(order.total)
    order.payment_status = Order.PaymentStatus.PAID
    if checkout_session_id:
        order.stripe_checkout_session_id = checkout_session_id
    if payment_intent_id:
        order.stripe_payment_intent_id = payment_intent_id
    order.platform_fee = platform_fee
    order.tailor_payout = (quantize_money(order.total) - platform_fee).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    order.paid_at = order.paid_at or timezone.now()
    order.save(update_fields=[
        'payment_status',
        'stripe_checkout_session_id',
        'stripe_payment_intent_id',
        'platform_fee',
        'tailor_payout',
        'paid_at',
        'updated_at',
    ])
    invalidate_api_cache()
    return order


def apply_refunded_state(order, *, refund_id=''):
    order.payment_status = Order.PaymentStatus.REFUNDED
    if refund_id:
        order.stripe_refund_id = refund_id
    order.refunded_at = order.refunded_at or timezone.now()
    order.save(update_fields=['payment_status', 'stripe_refund_id', 'refunded_at', 'updated_at'])
    invalidate_api_cache()
    return order


TAILOR_STATUS_FLOW = {
    Order.Status.PLACED: {Order.Status.RECEIVED, Order.Status.ACCEPTED, Order.Status.REJECTED},
    Order.Status.RECEIVED: {Order.Status.ACCEPTED, Order.Status.REJECTED},
    Order.Status.CONFIRMED: {Order.Status.IN_STITCHING, Order.Status.REJECTED},
    Order.Status.ACCEPTED: {Order.Status.IN_STITCHING, Order.Status.REJECTED},
    Order.Status.IN_STITCHING: {Order.Status.READY},
    Order.Status.READY: set(),
    Order.Status.OUT_FOR_DELIVERY: set(),
    Order.Status.DELIVERED: set(),
    Order.Status.REJECTED: set(),
    Order.Status.CANCELLED: set(),
}

ADMIN_STATUS_FLOW = {
    Order.Status.PLACED: {Order.Status.RECEIVED, Order.Status.ACCEPTED, Order.Status.REJECTED},
    Order.Status.RECEIVED: {Order.Status.ACCEPTED, Order.Status.REJECTED},
    Order.Status.CONFIRMED: {Order.Status.REJECTED},
    Order.Status.ACCEPTED: {Order.Status.REJECTED},
    Order.Status.REJECTED: set(),
    Order.Status.IN_STITCHING: set(),
    Order.Status.READY: set(),
    Order.Status.OUT_FOR_DELIVERY: set(),
    Order.Status.DELIVERED: set(),
    Order.Status.CANCELLED: set(),
}

DEBUG_TAILOR_SHOP_NAMES = {'Debug Shop', 'Debug Shop Json'}
DEBUG_DESIGN_TITLES = {'Debug Multipart Design'}


def get_public_tailor_queryset():
    return TailorProfile.objects.filter(is_active=True).exclude(shop_name__in=DEBUG_TAILOR_SHOP_NAMES)


def get_public_fabric_queryset():
    return Fabric.objects.filter(is_active=True).exclude(
        Q(shop__in=DEBUG_TAILOR_SHOP_NAMES)
        | Q(uploaded_by__tailor_profile__shop_name__in=DEBUG_TAILOR_SHOP_NAMES)
    )


def get_public_design_queryset():
    return (
        Design.objects.filter(Q(is_active=True) | Q(uploaded_by__role=User.Role.TAILOR))
        .exclude(title__in=DEBUG_DESIGN_TITLES)
        .exclude(
            Q(designer__in=DEBUG_TAILOR_SHOP_NAMES)
            | Q(uploaded_by__tailor_profile__shop_name__in=DEBUG_TAILOR_SHOP_NAMES)
        )
    )


def get_api_cache_version():
    return cache.get_or_set(CACHE_VERSION_KEY, 1, None)


def build_api_cache_key(namespace, *parts):
    digest = hashlib.sha256('||'.join(str(part) for part in parts).encode('utf-8')).hexdigest()
    return f'api:{namespace}:v{get_api_cache_version()}:{digest}'


def cached_response(namespace, request, ttl, builder, *, user_scoped=False):
    bypass_cache = str(request.headers.get('X-Bypass-Cache', '')).strip().lower() in {'1', 'true', 'yes'}
    if not bypass_cache:
        bypass_cache = str(request.query_params.get('fresh', '')).strip().lower() in {'1', 'true', 'yes'}

    if bypass_cache:
        return Response(builder())

    user_part = request.user.id if user_scoped and getattr(request.user, 'is_authenticated', False) else 'anon'
    cache_key = build_api_cache_key(namespace, request.get_full_path(), user_part)
    cached_payload = cache.get(cache_key)
    if cached_payload is not None:
        return Response(cached_payload)

    payload = builder()
    if should_cache_payload(payload):
        cache.set(cache_key, payload, ttl)
    return Response(payload)


def payload_contains_inline_image(value):
    if isinstance(value, str):
        return value.strip().lower().startswith('data:image/')
    if isinstance(value, dict):
        return any(payload_contains_inline_image(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(payload_contains_inline_image(item) for item in value)
    return False


def should_cache_payload(payload):
    if payload_contains_inline_image(payload):
        return False

    try:
        serialized = json.dumps(payload, separators=(',', ':'))
    except (TypeError, ValueError):
        return False

    return len(serialized.encode('utf-8')) <= MAX_CACHEABLE_PAYLOAD_BYTES


def invalidate_api_cache():
    try:
        cache.incr(CACHE_VERSION_KEY)
    except ValueError:
        cache.set(CACHE_VERSION_KEY, 2, None)


def normalize_order_status(status_value):
    if status_value == Order.Status.PLACED:
        return Order.Status.RECEIVED
    if status_value == Order.Status.CONFIRMED:
        return Order.Status.ACCEPTED
    return status_value


def update_order_status_with_rules(order, new_status, flow, *, actor_label):
    normalized_current = normalize_order_status(order.status)
    allowed_statuses = flow.get(order.status, set()) | flow.get(normalized_current, set())

    if new_status == normalized_current:
        if order.status != new_status and new_status in {Order.Status.RECEIVED, Order.Status.ACCEPTED}:
            order.status = new_status
            order.save(update_fields=['status', 'updated_at'])
        return order

    if new_status not in allowed_statuses:
        allowed_labels = ', '.join(sorted(allowed_statuses)) or 'no further updates'
        raise ValueError(f'{actor_label} cannot change this order from {normalized_current} to {new_status}. Allowed: {allowed_labels}.')

    order.status = new_status
    order.save(update_fields=['status', 'updated_at'])
    return order


class SignupView(APIView):
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        serializer = SignupSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        return Response(build_auth_payload(user), status=status.HTTP_201_CREATED)


class LoginView(APIView):
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        serializer = LoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.validated_data['user']
        
        # Create a session record for tracking (gracefully handle if table doesn't exist yet)
        try:
            user_agent = request.META.get('HTTP_USER_AGENT', '')
            x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR', '')
            ip_address = x_forwarded_for.split(',')[0].strip() if x_forwarded_for else request.META.get('REMOTE_ADDR', '')
            
            UserSession.objects.create(
                user=user,
                ip_address=ip_address,
                user_agent=user_agent
            )
        except Exception:
            # UserSession table may not exist yet on production - that's OK
            pass
        
        return Response(build_auth_payload(user))


class PasswordResetRequestView(APIView):
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        serializer = PasswordResetRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        email = serializer.validated_data['email']
        user = User.objects.filter(email__iexact=email).first()

        if user:
            otp = f'{random.SystemRandom().randint(0, 999999):06d}'
            expiry_minutes = getattr(settings, 'PASSWORD_RESET_OTP_EXPIRY_MINUTES', 10)

            email_backend = str(getattr(settings, 'EMAIL_BACKEND', ''))
            using_smtp = email_backend == 'django.core.mail.backends.smtp.EmailBackend'
            if using_smtp and (not getattr(settings, 'EMAIL_HOST_USER', '') or not getattr(settings, 'EMAIL_HOST_PASSWORD', '')):
                logger.error('Password reset email is not configured: EMAIL_HOST_USER or EMAIL_HOST_PASSWORD is missing.')
                return Response(
                    {'detail': 'Password reset email is not configured on the server.'},
                    status=status.HTTP_503_SERVICE_UNAVAILABLE,
                )

            try:
                with transaction.atomic():
                    PasswordResetOTP.objects.filter(user=user, consumed_at__isnull=True).update(consumed_at=timezone.now())
                    PasswordResetOTP.objects.create(
                        user=user,
                        otp_hash=PasswordResetOTP.hash_otp(otp),
                        expires_at=timezone.now() + timedelta(minutes=expiry_minutes),
                    )
            except DatabaseError:
                logger.exception('Password reset OTP database operation failed. Check that migrations are applied.')
                return Response(
                    {'detail': 'Password reset is not ready on the server. Please run backend migrations.'},
                    status=status.HTTP_503_SERVICE_UNAVAILABLE,
                )

            try:
                send_mail(
                    'Your FASS password reset code',
                    f'Your FASS password reset OTP is {otp}. It expires in {expiry_minutes} minutes.',
                    getattr(settings, 'DEFAULT_FROM_EMAIL', None),
                    [user.email],
                    fail_silently=False,
                )
            except (smtplib.SMTPException, OSError):
                logger.exception('Password reset OTP email delivery failed for user id %s.', user.id)
                return Response(
                    {'detail': 'Could not send OTP email. Please check the server email settings.'},
                    status=status.HTTP_503_SERVICE_UNAVAILABLE,
                )

        return Response({'detail': 'If an account exists for this email, an OTP has been sent.'})


class PasswordResetConfirmView(APIView):
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        serializer = PasswordResetConfirmSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.validated_data.get('user')
        generic_error = {'detail': 'Invalid or expired OTP.'}

        if not user:
            return Response(generic_error, status=status.HTTP_400_BAD_REQUEST)

        reset_otp = (
            PasswordResetOTP.objects.filter(user=user, consumed_at__isnull=True)
            .order_by('-created_at')
            .first()
        )
        max_attempts = getattr(settings, 'PASSWORD_RESET_OTP_MAX_ATTEMPTS', 5)

        if not reset_otp or reset_otp.is_expired or reset_otp.attempts >= max_attempts:
            return Response(generic_error, status=status.HTTP_400_BAD_REQUEST)

        if not reset_otp.check_otp(serializer.validated_data['otp']):
            reset_otp.attempts += 1
            reset_otp.save(update_fields=['attempts'])
            return Response(generic_error, status=status.HTTP_400_BAD_REQUEST)

        user.set_password(serializer.validated_data['new_password'])
        user.save(update_fields=['password'])
        reset_otp.consumed_at = timezone.now()
        reset_otp.save(update_fields=['consumed_at'])
        Token.objects.filter(user=user).delete()

        return Response({'detail': 'Password has been reset successfully.'})


class ProfileView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    parser_classes = IMAGE_UPLOAD_PARSER_CLASSES

    def get(self, request):
        return Response(UserSerializer(request.user, context={'request': request}).data)

    def patch(self, request):
        user = request.user
        allowed_fields = ('full_name', 'phone', 'address')
        update_fields = []

        for field in allowed_fields:
            if field in request.data:
                setattr(user, field, str(request.data.get(field, '')).strip())
                update_fields.append(field)

        uploaded_image_file = request.FILES.get('image_file') if request else None
        if not uploaded_image_file and request:
            uploaded_image_files = request.FILES.getlist('image_files')
            uploaded_image_file = uploaded_image_files[0] if uploaded_image_files else None
        if uploaded_image_file:
            user.image, _ = sync_uploaded_files_or_raise(
                uploaded_image_file,
                [uploaded_image_file],
                folder='user-profiles',
                field_name='image',
            )
            update_fields.append('image')
        elif 'image' in request.data:
            optimized_image = optimize_inline_image(str(request.data.get('image', '')).strip(), field_name='image')
            user.image, _ = sync_cloudinary_images_or_raise(
                optimized_image,
                [],
                folder='user-profiles',
                field_name='image',
            )
            update_fields.append('image')

        user.save(update_fields=update_fields or None)
        invalidate_api_cache()
        return Response(UserSerializer(user, context={'request': request}).data)


class CustomerDashboardView(APIView):
    permission_classes = [permissions.IsAuthenticated, IsCustomer]

    def get(self, request):
        return cached_response(
            'customer-dashboard',
            request,
            USER_CACHE_TTL,
            lambda: DashboardSerializer(
                {
                    'top_tailors': get_public_tailor_queryset()
                    .filter(is_featured=True)
                    .select_related('user')
                    .only(
                        'user__id',
                        'user__full_name',
                        'specialty',
                        'location',
                        'image',
                        'rating',
                        'service_price',
                        'is_featured',
                        'is_active',
                    )[:10],
                    'fabrics': get_public_fabric_queryset()
                    .only('id', 'material', 'color', 'price', 'image', 'images', 'shop', 'description', 'is_active')
                    .order_by('-created_at')[:10],
                    'measurements': MeasurementProfile.objects.filter(customer=request.user).only(
                        'id',
                        'name',
                        'chest',
                        'waist',
                        'shoulder',
                        'sleeve',
                        'height',
                        'length',
                        'is_default',
                        'created_at',
                    )[:10],
                    'recent_orders': Order.objects.filter(customer=request.user)
                    .select_related('tailor', 'design')
                    .only(
                        'id',
                        'status',
                        'payment_method',
                        'payment_status',
                        'total',
                        'estimated_completion_date',
                        'created_at',
                        'tailor__full_name',
                        'design__title',
                    )[:10],
                    'designs': get_public_design_queryset()
                    .select_related('uploaded_by', 'uploaded_by__tailor_profile')
                    .only(
                        'id',
                        'title',
                        'category',
                        'image',
                        'images',
                        'description',
                        'compatible_fabrics',
                        'designer',
                        'uploaded_by__id',
                        'uploaded_by__role',
                        'uploaded_by__full_name',
                        'uploaded_by__tailor_profile__shop_name',
                        'base_price',
                        'is_active',
                        'created_at',
                    )
                    .order_by('-created_at')[:10],
                }
            ).data,
            user_scoped=True,
        )


class TailorListView(generics.ListAPIView):
    serializer_class = PublicTailorSerializer
    permission_classes = [permissions.AllowAny]

    def get_queryset(self):
        queryset = (
            get_public_tailor_queryset()
            .select_related('user')
            .only(
                'user__id',
                'user__full_name',
                'rating',
                'specialty',
                'location',
                'eta',
                'image',
                'about',
                'service_price',
                'is_featured',
                'is_active',
                'shop_name',
            )
        )
        if self.request.query_params.get('top') == '1':
            queryset = queryset.filter(is_featured=True)
        return queryset

    def list(self, request, *args, **kwargs):
        return cached_response(
            'tailor-list',
            request,
            PUBLIC_CACHE_TTL,
            lambda: self.get_serializer(self.get_queryset(), many=True).data,
        )


class TailorDetailView(generics.RetrieveAPIView):
    queryset = (
        get_public_tailor_queryset()
        .select_related('user')
        .only(
            'user__id',
            'user__full_name',
            'rating',
            'specialty',
            'location',
            'eta',
            'image',
            'about',
            'service_price',
            'is_featured',
            'is_active',
            'shop_name',
        )
    )
    serializer_class = PublicTailorSerializer
    permission_classes = [permissions.AllowAny]
    lookup_field = 'user_id'
    lookup_url_kwarg = 'pk'

    def retrieve(self, request, *args, **kwargs):
        return cached_response(
            'tailor-detail',
            request,
            PUBLIC_CACHE_TTL,
            lambda: self.get_serializer(self.get_object()).data,
        )


class TailorShopCatalogView(APIView):
    permission_classes = [permissions.AllowAny]

    def get(self, request, pk):
        def build_payload():
            tailor = get_object_or_404(
                get_public_tailor_queryset()
                .select_related('user')
                .only(
                    'user__id',
                    'user__full_name',
                    'rating',
                    'specialty',
                    'location',
                    'eta',
                    'image',
                    'about',
                    'service_price',
                    'is_featured',
                    'is_active',
                    'shop_name',
                ),
                user_id=pk,
            )
            payload = {
                'tailor': tailor,
                'fabrics': get_public_fabric_queryset()
                .filter(uploaded_by_id=pk)
                .only('id', 'material', 'color', 'price', 'image', 'images', 'shop', 'description', 'is_active')
                .order_by('-created_at'),
                'designs': get_public_design_queryset()
                .filter(uploaded_by_id=pk)
                .select_related('uploaded_by', 'uploaded_by__tailor_profile')
                .only(
                    'id',
                    'title',
                    'category',
                    'image',
                    'images',
                    'description',
                    'compatible_fabrics',
                    'designer',
                    'uploaded_by__id',
                    'uploaded_by__role',
                    'uploaded_by__full_name',
                    'uploaded_by__tailor_profile__shop_name',
                    'base_price',
                    'is_active',
                    'created_at',
                )
                .order_by('-created_at'),
            }
            return TailorShopCatalogSerializer(payload).data

        return cached_response('tailor-catalog', request, PUBLIC_CACHE_TTL, build_payload)


class TailorMeView(APIView):
    permission_classes = [permissions.IsAuthenticated, IsTailor]
    parser_classes = IMAGE_UPLOAD_PARSER_CLASSES

    def get_profile(self, user):
        profile, _ = TailorProfile.objects.get_or_create(user=user)
        return profile

    def get(self, request):
        profile = self.get_profile(request.user)
        return cached_response(
            'tailor-me',
            request,
            USER_CACHE_TTL,
            lambda: TailorProfileSerializer(profile).data,
            user_scoped=True,
        )

    def patch(self, request):
        profile = self.get_profile(request.user)
        serializer = TailorShopSetupSerializer(profile, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        profile = serializer.save()
        invalidate_api_cache()
        return Response(TailorProfileSerializer(profile).data)


class DriverMeView(APIView):
    permission_classes = [permissions.IsAuthenticated, IsDriver]

    def get_profile(self, user):
        profile, _ = DriverProfile.objects.get_or_create(user=user)
        return profile

    def get(self, request):
        profile = self.get_profile(request.user)
        return cached_response(
            'driver-me',
            request,
            USER_CACHE_TTL,
            lambda: DriverProfileSerializer(profile).data,
            user_scoped=True,
        )

    def patch(self, request):
        profile = self.get_profile(request.user)
        serializer = DriverProfileUpdateSerializer(profile, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        profile = serializer.save()
        invalidate_api_cache()
        return Response(DriverProfileSerializer(profile).data)


class FabricListView(generics.ListCreateAPIView):
    queryset = get_public_fabric_queryset()
    serializer_class = DashboardFabricSerializer
    permission_classes = [permissions.AllowAny]
    parser_classes = IMAGE_UPLOAD_PARSER_CLASSES

    def get_serializer_class(self):
        if str(getattr(self.request, 'method', '')).upper() == 'POST':
            return FabricSerializer
        return DashboardFabricSerializer

    def get_queryset(self):
        return (
            super()
            .get_queryset()
            .only('id', 'material', 'color', 'price', 'image', 'images', 'shop', 'description', 'is_active')
            .order_by('-created_at')
        )

    def list(self, request, *args, **kwargs):
        return cached_response(
            'fabric-list',
            request,
            PUBLIC_CACHE_TTL,
            lambda: self.get_serializer(self.get_queryset(), many=True).data,
        )

    def perform_create(self, serializer):
        serializer.save()
        invalidate_api_cache()


class FabricDetailView(generics.RetrieveAPIView):
    queryset = (
        get_public_fabric_queryset()
        .only('id', 'material', 'color', 'price', 'image', 'images', 'shop', 'description', 'is_active')
    )
    serializer_class = DashboardFabricSerializer
    permission_classes = [permissions.AllowAny]

    def retrieve(self, request, *args, **kwargs):
        return cached_response(
            'fabric-detail',
            request,
            PUBLIC_CACHE_TTL,
            lambda: self.get_serializer(self.get_object()).data,
        )


class TailorFabricListCreateView(generics.ListCreateAPIView):
    serializer_class = FabricSerializer
    permission_classes = [permissions.IsAuthenticated, IsTailor]
    parser_classes = IMAGE_UPLOAD_PARSER_CLASSES

    def get_queryset(self):
        return Fabric.objects.filter(uploaded_by=self.request.user).order_by('-created_at')

    def list(self, request, *args, **kwargs):
        return cached_response(
            'tailor-fabrics',
            request,
            USER_CACHE_TTL,
            lambda: self.get_serializer(self.get_queryset(), many=True).data,
            user_scoped=True,
        )

    def perform_create(self, serializer):
        serializer.save()
        invalidate_api_cache()


class TailorFabricDetailView(generics.RetrieveDestroyAPIView):
    serializer_class = FabricSerializer
    permission_classes = [permissions.IsAuthenticated, IsTailor]

    def get_queryset(self):
        return Fabric.objects.filter(uploaded_by=self.request.user)

    def destroy(self, request, *args, **kwargs):
        response = super().destroy(request, *args, **kwargs)
        invalidate_api_cache()
        return response


class DesignListView(generics.ListCreateAPIView):
    queryset = get_public_design_queryset()
    serializer_class = DashboardDesignSerializer
    permission_classes = [permissions.IsAuthenticatedOrReadOnly]
    parser_classes = IMAGE_UPLOAD_PARSER_CLASSES

    def get_serializer_class(self):
        if str(getattr(self.request, 'method', '')).upper() == 'POST':
            return DesignSerializer
        return DashboardDesignSerializer

    def get_queryset(self):
        return (
            super()
            .get_queryset()
            .select_related('uploaded_by', 'uploaded_by__tailor_profile')
            .only(
                'id',
                'title',
                'category',
                'image',
                'images',
                'description',
                'compatible_fabrics',
                'designer',
                'uploaded_by__id',
                'uploaded_by__role',
                'uploaded_by__full_name',
                'uploaded_by__tailor_profile__shop_name',
                'base_price',
                'is_active',
                'created_at',
            )
            .order_by('-created_at')
        )

    def list(self, request, *args, **kwargs):
        return cached_response(
            'design-list',
            request,
            PUBLIC_CACHE_TTL,
            lambda: self.get_serializer(self.get_queryset(), many=True).data,
        )

    def perform_create(self, serializer):
        serializer.save()
        invalidate_api_cache()


class DesignDetailView(generics.RetrieveAPIView):
    queryset = (
        get_public_design_queryset()
        .select_related('uploaded_by', 'uploaded_by__tailor_profile')
        .only(
            'id',
            'title',
            'category',
            'image',
            'images',
            'description',
            'compatible_fabrics',
            'designer',
            'uploaded_by__id',
            'uploaded_by__role',
            'uploaded_by__full_name',
            'uploaded_by__tailor_profile__shop_name',
            'base_price',
            'is_active',
            'created_at',
        )
    )
    serializer_class = DashboardDesignSerializer
    permission_classes = [permissions.AllowAny]

    def retrieve(self, request, *args, **kwargs):
        return cached_response(
            'design-detail',
            request,
            PUBLIC_CACHE_TTL,
            lambda: self.get_serializer(self.get_object()).data,
        )


class TailorDesignListCreateView(generics.ListCreateAPIView):
    serializer_class = DesignSerializer
    permission_classes = [permissions.IsAuthenticated, IsTailor]
    parser_classes = IMAGE_UPLOAD_PARSER_CLASSES

    def get_queryset(self):
        return (
            Design.objects.filter(uploaded_by=self.request.user)
            .select_related('uploaded_by', 'uploaded_by__tailor_profile')
            .order_by('-created_at')
        )

    def list(self, request, *args, **kwargs):
        return cached_response(
            'tailor-designs',
            request,
            USER_CACHE_TTL,
            lambda: self.get_serializer(self.get_queryset(), many=True).data,
            user_scoped=True,
        )

    def perform_create(self, serializer):
        serializer.save(is_active=True)
        invalidate_api_cache()


class TailorDesignDetailView(generics.RetrieveDestroyAPIView):
    serializer_class = DesignSerializer
    permission_classes = [permissions.IsAuthenticated, IsTailor]

    def get_queryset(self):
        return Design.objects.filter(uploaded_by=self.request.user).select_related('uploaded_by', 'uploaded_by__tailor_profile')

    def destroy(self, request, *args, **kwargs):
        response = super().destroy(request, *args, **kwargs)
        invalidate_api_cache()
        return response


class MeasurementListCreateView(generics.ListCreateAPIView):
    serializer_class = MeasurementSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return MeasurementProfile.objects.filter(customer=self.request.user)

    def list(self, request, *args, **kwargs):
        return cached_response(
            'measurement-list',
            request,
            USER_CACHE_TTL,
            lambda: self.get_serializer(self.get_queryset(), many=True).data,
            user_scoped=True,
        )

    def perform_create(self, serializer):
        serializer.save()
        invalidate_api_cache()


class MeasurementDetailView(generics.RetrieveUpdateDestroyAPIView):
    serializer_class = MeasurementSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return MeasurementProfile.objects.filter(customer=self.request.user)

    def perform_update(self, serializer):
        serializer.save()
        invalidate_api_cache()

    def perform_destroy(self, instance):
        instance.delete()
        invalidate_api_cache()


class OrderListCreateView(generics.ListCreateAPIView):
    serializer_class = OrderSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        queryset = Order.objects.select_related('customer', 'tailor', 'design', 'fabric', 'measurement', 'delivery', 'delivery__driver')
        if user.role == User.Role.TAILOR:
            return queryset.filter(tailor=user)
        return queryset.filter(customer=user)

    def list(self, request, *args, **kwargs):
        return cached_response(
            'order-list',
            request,
            USER_CACHE_TTL,
            lambda: self.get_serializer(self.get_queryset(), many=True).data,
            user_scoped=True,
        )

    def perform_create(self, serializer):
        serializer.save()
        invalidate_api_cache()


class OrderDetailView(generics.RetrieveAPIView):
    serializer_class = OrderSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        queryset = Order.objects.select_related('customer', 'tailor', 'design', 'fabric', 'measurement', 'delivery', 'delivery__driver')
        if user.is_staff:
            return queryset
        if user.role == User.Role.TAILOR:
            return queryset.filter(tailor=user)
        return queryset.filter(customer=user)

    def retrieve(self, request, *args, **kwargs):
        return cached_response(
            'order-detail',
            request,
            USER_CACHE_TTL,
            lambda: self.get_serializer(self.get_object()).data,
            user_scoped=True,
        )


class StripeCheckoutSessionView(APIView):
    permission_classes = [permissions.IsAuthenticated, IsCustomer]

    @staticmethod
    def build_redirect_url(base_url, session_placeholder=True):
        url = str(base_url or '').strip()
        if not url:
            return ''
        placeholder = '{CHECKOUT_SESSION_ID}'
        if not session_placeholder or placeholder in url:
            return url
        separator = '&' if '?' in url else '?'
        return f'{url}{separator}stripe_session_id={placeholder}'

    def post(self, request):
        order_id = request.data.get('order_id')
        order = get_object_or_404(
            Order.objects.select_related('customer', 'tailor', 'design', 'fabric'),
            pk=order_id,
            customer=request.user,
        )

        if order.payment_status == Order.PaymentStatus.PAID:
            return Response({'detail': 'This order is already paid.'}, status=status.HTTP_400_BAD_REQUEST)
        if order.payment_status == Order.PaymentStatus.REFUNDED:
            return Response({'detail': 'This order has already been refunded.'}, status=status.HTTP_400_BAD_REQUEST)
        if order.payment_method not in ONLINE_PAYMENT_METHODS:
            return Response({'detail': 'Stripe checkout is only available for card or wallet payments.'}, status=status.HTTP_400_BAD_REQUEST)
        if stripe_amount_from_decimal(order.total) <= 0:
            return Response({'detail': 'Order total must be greater than zero.'}, status=status.HTTP_400_BAD_REQUEST)

        success_url = self.build_redirect_url(request.data.get('success_url'))
        cancel_url = self.build_redirect_url(request.data.get('cancel_url'), session_placeholder=False)
        if not success_url or not cancel_url:
            return Response({'detail': 'Success and cancel URLs are required.'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            stripe = get_stripe_module()
        except RuntimeError as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)

        order_title = order.design.title if order.design else f'FASS Order #{order.id}'
        fabric_label = order.fabric.material if order.fabric else 'Custom fabric'
        platform_fee = calculate_platform_fee(order.total)
        tailor_payout = (quantize_money(order.total) - platform_fee).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

        try:
            checkout_session = stripe.checkout.Session.create(
                mode='payment',
                payment_method_types=['card'],
                customer_email=order.customer.email,
                client_reference_id=str(order.id),
                success_url=success_url,
                cancel_url=cancel_url,
                line_items=[
                    {
                        'quantity': 1,
                        'price_data': {
                            'currency': getattr(settings, 'STRIPE_CURRENCY', 'aed'),
                            'unit_amount': stripe_amount_from_decimal(order.total),
                            'product_data': {
                                'name': order_title,
                                'description': f'{fabric_label} tailoring order with {order.tailor.full_name}',
                            },
                        },
                    }
                ],
                metadata={
                    'order_id': str(order.id),
                    'customer_id': str(order.customer_id),
                    'tailor_id': str(order.tailor_id),
                    'platform_fee': str(platform_fee),
                    'tailor_payout': str(tailor_payout),
                },
                payment_intent_data={
                    'metadata': {
                        'order_id': str(order.id),
                        'customer_id': str(order.customer_id),
                        'tailor_id': str(order.tailor_id),
                    },
                },
            )
        except Exception as exc:
            logger.exception('Stripe checkout session creation failed for order %s.', order.id)
            return Response({'detail': f'Could not start Stripe checkout: {exc}'}, status=status.HTTP_502_BAD_GATEWAY)

        order.stripe_checkout_session_id = checkout_session.id
        order.platform_fee = platform_fee
        order.tailor_payout = tailor_payout
        order.save(update_fields=['stripe_checkout_session_id', 'platform_fee', 'tailor_payout', 'updated_at'])
        invalidate_api_cache()

        return Response({
            'order_id': order.id,
            'checkout_session_id': checkout_session.id,
            'url': checkout_session.url,
            'publishable_key': getattr(settings, 'STRIPE_PUBLISHABLE_KEY', ''),
            'payment_status': order.payment_status,
            'platform_fee': platform_fee,
            'tailor_payout': tailor_payout,
        })


class StripePaymentStatusView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        session_id = str(request.data.get('checkout_session_id') or request.data.get('stripe_session_id') or '').strip()
        order_id = request.data.get('order_id')

        queryset = Order.objects.select_related('customer', 'tailor')
        if request.user.is_staff:
            order_queryset = queryset
        else:
            order_queryset = queryset.filter(Q(customer=request.user) | Q(tailor=request.user))

        order = None
        if order_id:
            order = get_object_or_404(order_queryset, pk=order_id)
        elif session_id:
            order = get_object_or_404(order_queryset, stripe_checkout_session_id=session_id)
        else:
            return Response({'detail': 'Order ID or checkout session ID is required.'}, status=status.HTTP_400_BAD_REQUEST)

        if session_id:
            try:
                stripe = get_stripe_module()
                session = stripe.checkout.Session.retrieve(session_id)
            except RuntimeError as exc:
                return Response({'detail': str(exc)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
            except Exception as exc:
                logger.exception('Stripe checkout session retrieve failed for %s.', session_id)
                return Response({'detail': f'Could not verify Stripe payment: {exc}'}, status=status.HTTP_502_BAD_GATEWAY)

            if getattr(session, 'payment_status', '') == 'paid':
                apply_paid_state(
                    order,
                    payment_intent_id=str(getattr(session, 'payment_intent', '') or ''),
                    checkout_session_id=session_id,
                )
            elif getattr(session, 'payment_status', '') == 'unpaid' and order.payment_status == Order.PaymentStatus.PENDING:
                order.payment_status = Order.PaymentStatus.PENDING
                order.save(update_fields=['payment_status', 'updated_at'])

        refreshed_order = order_queryset.get(pk=order.pk)
        return Response(OrderSerializer(refreshed_order).data)


class StripeCheckoutReturnView(APIView):
    permission_classes = [permissions.AllowAny]
    authentication_classes = []

    def get(self, request):
        result = str(request.query_params.get('payment') or request.query_params.get('result') or 'success').strip().lower()
        if result not in {'success', 'cancel'}:
            result = 'success'

        order_id = str(request.query_params.get('order_id') or request.query_params.get('orderId') or '').strip()
        session_id = str(request.query_params.get('stripe_session_id') or request.query_params.get('session_id') or '').strip()
        app_url = f'fass://orders?payment={result}'
        if order_id:
            app_url += f'&orderId={order_id}'
        if session_id:
            app_url += f'&stripe_session_id={session_id}'

        escaped_app_url = json.dumps(app_url)
        return HttpResponse(
            f"""<!doctype html>
<html>
  <head>
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>Returning to FASS</title>
  </head>
  <body style="font-family: Arial, sans-serif; padding: 24px;">
    <h2>Returning to FASS...</h2>
    <p>If the app does not open automatically, tap the button below.</p>
    <p><a href={escaped_app_url} style="display:inline-block;padding:12px 16px;background:#9cc5c9;color:#111;text-decoration:none;border-radius:8px;">Open FASS</a></p>
    <script>
      window.location.href = {escaped_app_url};
    </script>
  </body>
</html>""",
            content_type='text/html',
        )


@method_decorator(csrf_exempt, name='dispatch')
class StripeWebhookView(APIView):
    permission_classes = [permissions.AllowAny]
    authentication_classes = []

    def post(self, request):
        webhook_secret = getattr(settings, 'STRIPE_WEBHOOK_SECRET', '')
        if not webhook_secret:
            return Response({'detail': 'Stripe webhook secret is not configured.'}, status=status.HTTP_503_SERVICE_UNAVAILABLE)

        try:
            stripe = get_stripe_module()
        except RuntimeError as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)

        signature = request.META.get('HTTP_STRIPE_SIGNATURE', '')
        try:
            event = stripe.Webhook.construct_event(request.body, signature, webhook_secret)
        except ValueError:
            return Response({'detail': 'Invalid Stripe webhook payload.'}, status=status.HTTP_400_BAD_REQUEST)
        except Exception:
            logger.exception('Stripe webhook signature verification failed.')
            return Response({'detail': 'Invalid Stripe webhook signature.'}, status=status.HTTP_400_BAD_REQUEST)

        event_type = event.get('type')
        event_object = event.get('data', {}).get('object', {})

        if event_type == 'checkout.session.completed':
            order_id = event_object.get('metadata', {}).get('order_id') or event_object.get('client_reference_id')
            if order_id and event_object.get('payment_status') == 'paid':
                order = Order.objects.filter(pk=order_id).first()
                if order:
                    apply_paid_state(
                        order,
                        payment_intent_id=str(event_object.get('payment_intent') or ''),
                        checkout_session_id=str(event_object.get('id') or ''),
                    )
        elif event_type == 'payment_intent.succeeded':
            order_id = event_object.get('metadata', {}).get('order_id')
            if order_id:
                order = Order.objects.filter(pk=order_id).first()
                if order:
                    apply_paid_state(order, payment_intent_id=str(event_object.get('id') or ''))
        elif event_type in {'charge.refunded', 'refund.updated'}:
            payment_intent_id = str(event_object.get('payment_intent') or '')
            refund_id = str(event_object.get('id') or '')
            if payment_intent_id:
                order = Order.objects.filter(stripe_payment_intent_id=payment_intent_id).first()
                if order:
                    apply_refunded_state(order, refund_id=refund_id)

        return Response({'received': True})


class TailorOrderListView(generics.ListAPIView):
    serializer_class = TailorOrderListSerializer
    permission_classes = [permissions.IsAuthenticated, IsTailor]

    def get_queryset(self):
        return (
            Order.objects.filter(tailor=self.request.user)
            .select_related('customer', 'design', 'fabric')
            .only(
                'id',
                'customer__full_name',
                'customer_phone',
                'design__title',
                'design__image',
                'design__images',
                'fabric__material',
                'fabric__color',
                'fabric__image',
                'fabric__images',
                'garment_type',
                'notes',
                'status',
                'total',
                'estimated_completion_date',
                'created_at',
            )
        )

    def list(self, request, *args, **kwargs):
        return cached_response(
            'tailor-order-list',
            request,
            USER_CACHE_TTL,
            lambda: self.get_serializer(self.get_queryset(), many=True).data,
            user_scoped=True,
        )


class TailorOrderDetailUpdateView(generics.RetrieveUpdateAPIView):
    serializer_class = TailorOrderDetailSerializer
    permission_classes = [permissions.IsAuthenticated, IsTailor]

    def get_queryset(self):
        return Order.objects.filter(tailor=self.request.user).select_related('customer', 'tailor', 'design', 'fabric', 'measurement', 'delivery')

    def patch(self, request, *args, **kwargs):
        order = self.get_object()
        new_status = str(request.data.get('status', '')).strip()
        estimated_completion_value = str(request.data.get('estimated_completion_date', '')).strip()

        if not new_status:
            return Response({'detail': 'Status is required.'}, status=status.HTTP_400_BAD_REQUEST)

        if new_status not in Order.Status.values:
            return Response({'detail': 'Invalid order status.'}, status=status.HTTP_400_BAD_REQUEST)

        if estimated_completion_value:
            estimated_completion_date = parse_date(estimated_completion_value)
            if estimated_completion_date is None:
                return Response(
                    {'detail': 'Estimated completion date must use YYYY-MM-DD format.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            order.estimated_completion_date = estimated_completion_date
            order.save(update_fields=['estimated_completion_date', 'updated_at'])

        normalized_status = normalize_order_status(new_status)
        if (
            normalized_status == Order.Status.ACCEPTED
            and order.payment_method in ONLINE_PAYMENT_METHODS
            and order.payment_status != Order.PaymentStatus.PAID
        ):
            return Response(
                {'detail': 'Online payment must be completed before accepting this order.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if normalized_status == Order.Status.ACCEPTED and not order.estimated_completion_date:
            return Response(
                {'detail': 'Estimated completion date is required before accepting this order.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            update_order_status_with_rules(order, new_status, TAILOR_STATUS_FLOW, actor_label='Tailor')
        except ValueError as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        refreshed_order = self.get_queryset().get(pk=order.pk)
        invalidate_api_cache()
        return Response(OrderSerializer(refreshed_order).data)


class DriverDeliveryListView(generics.ListAPIView):
    serializer_class = DeliverySerializer
    permission_classes = [permissions.IsAuthenticated, IsDriver]

    def get_queryset(self):
        return Delivery.objects.filter(driver=self.request.user).select_related('order', 'order__customer', 'order__tailor', 'driver')

    def list(self, request, *args, **kwargs):
        return cached_response(
            'driver-deliveries',
            request,
            USER_CACHE_TTL,
            lambda: self.get_serializer(self.get_queryset(), many=True).data,
            user_scoped=True,
        )


class DriverDeliveryDetailUpdateView(generics.RetrieveUpdateAPIView):
    serializer_class = DeliverySerializer
    permission_classes = [permissions.IsAuthenticated, IsDriver]

    def get_queryset(self):
        return Delivery.objects.filter(driver=self.request.user).select_related('order', 'order__customer', 'order__tailor', 'driver')

    def perform_update(self, serializer):
        previous_status = serializer.instance.status
        delivery = serializer.save()
        order = delivery.order

        if previous_status == delivery.status:
            return

        update_fields = ['updated_at']

        if delivery.status == Delivery.Status.PICKED_UP:
            if not delivery.pickup_time:
                delivery.pickup_time = timezone.now()
                delivery.save(update_fields=['pickup_time'])
            if order.status != Order.Status.OUT_FOR_DELIVERY:
                order.status = Order.Status.OUT_FOR_DELIVERY
                update_fields.append('status')

        elif delivery.status == Delivery.Status.IN_TRANSIT:
            if order.status != Order.Status.OUT_FOR_DELIVERY:
                order.status = Order.Status.OUT_FOR_DELIVERY
                update_fields.append('status')

        elif delivery.status == Delivery.Status.DELIVERED:
            if not delivery.delivery_time:
                delivery.delivery_time = timezone.now()
                delivery.save(update_fields=['delivery_time'])
            if order.status != Order.Status.DELIVERED:
                order.status = Order.Status.DELIVERED
                order.payment_status = Order.PaymentStatus.PAID if order.payment_method == Order.PaymentMethod.CASH else order.payment_status
                update_fields.extend(['status', 'payment_status'])

        if previous_status != delivery.status or len(update_fields) > 1:
            order.save(update_fields=list(dict.fromkeys(update_fields)))
        invalidate_api_cache()


class NotificationListView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        return cached_response(
            'notifications',
            request,
            USER_CACHE_TTL,
            lambda: NotificationSerializer(
                [
                    {
                        'id': f'order-{order.id}',
                        'title': f'Order #{order.id} update',
                        'message': f'Your order with {order.tailor.full_name} is currently {order.status}.',
                        'created_at': order.updated_at,
                    }
                    for order in Order.objects.filter(customer=request.user).select_related('tailor')[:10]
                ],
                many=True,
            ).data,
            user_scoped=True,
        )


class InvoiceListView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        return cached_response(
            'invoice-list',
            request,
            USER_CACHE_TTL,
            lambda: InvoiceSerializer(
                [
                    {
                        'id': f'INV-{order.id}',
                        'order_id': order.id,
                        'customer_name': order.customer.full_name,
                        'tailor_name': order.tailor.full_name,
                        'total': order.total,
                        'payment_method': order.payment_method,
                        'payment_status': order.payment_status,
                        'created_at': order.created_at,
                    }
                    for order in Order.objects.filter(customer=request.user).select_related('tailor')[:20]
                ],
                many=True,
            ).data,
            user_scoped=True,
        )


class InvoiceDetailView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, invoice_id):
        def build_payload():
            try:
                order_id = int(str(invoice_id).replace('INV-', ''))
            except ValueError:
                raise ValueError('Invoice not found.')

            order = get_object_or_404(
                Order.objects.select_related('customer', 'tailor'),
                pk=order_id,
                customer=request.user,
            )
            payload = {
                'id': f'INV-{order.id}',
                'order_id': order.id,
                'customer_name': order.customer.full_name,
                'tailor_name': order.tailor.full_name,
                'total': order.total,
                'payment_method': order.payment_method,
                'payment_status': order.payment_status,
                'created_at': order.created_at,
            }
            return InvoiceSerializer(payload).data

        try:
            return cached_response('invoice-detail', request, USER_CACHE_TTL, build_payload, user_scoped=True)
        except ValueError:
            return Response({'detail': 'Invoice not found.'}, status=status.HTTP_404_NOT_FOUND)


class AdminOverviewView(APIView):
    permission_classes = [permissions.IsAuthenticated, permissions.IsAdminUser]

    def get(self, request):
        def build_payload():
            all_orders = Order.objects.all()
            paid_orders = all_orders.filter(payment_status=Order.PaymentStatus.PAID)
            today = timezone.localdate()
            orders_last_7_days = all_orders.filter(created_at__date__gte=today - timedelta(days=6))
            orders_last_30_days = all_orders.filter(created_at__date__gte=today - timedelta(days=29))
            status_labels = dict(Order.Status.choices)
            payment_labels = dict(Order.PaymentMethod.choices)

            gross_revenue = all_orders.aggregate(total=Sum('total'))['total'] or 0
            paid_revenue = paid_orders.aggregate(total=Sum('total'))['total'] or 0
            average_order_value = all_orders.aggregate(value=Avg('total'))['value'] or 0
            average_paid_order_value = paid_orders.aggregate(value=Avg('total'))['value'] or 0
            payment_breakdown = list(
                all_orders
                .values('payment_method')
                .annotate(count=Count('id'), amount=Sum('total'))
                .order_by('-count', 'payment_method')
            )
            status_breakdown = list(
                all_orders
                .values('status')
                .annotate(count=Count('id'))
                .order_by('-count', 'status')
            )
            top_payment_method = payment_breakdown[0] if payment_breakdown else None
            unique_buyers = all_orders.values('customer_id').distinct().count()
            repeat_buyers = (
                all_orders
                .values('customer_id')
                .annotate(order_count=Count('id'))
                .filter(order_count__gt=1)
                .count()
            )

            return {
                'counts': {
                    'total_users': User.objects.filter(is_staff=False).count(),
                    'customers': User.objects.filter(role=User.Role.CUSTOMER).count(),
                    'tailors': User.objects.filter(role=User.Role.TAILOR).count(),
                    'drivers': User.objects.filter(role=User.Role.DRIVER).count(),
                    'orders': all_orders.count(),
                    'pending_assignments': Delivery.objects.filter(status=Delivery.Status.PENDING_ASSIGNMENT).count(),
                    'featured_tailors': TailorProfile.objects.filter(is_featured=True, is_active=True).count(),
                    'available_drivers': DriverProfile.objects.filter(is_available=True).count(),
                    'fabrics': Fabric.objects.count(),
                    'designs': Design.objects.count(),
                },
                'insights': {
                    'orders_today': all_orders.filter(created_at__date=today).count(),
                    'orders_last_7_days': orders_last_7_days.count(),
                    'orders_last_30_days': orders_last_30_days.count(),
                    'gross_revenue': float(gross_revenue),
                    'paid_revenue': float(paid_revenue),
                    'revenue_last_30_days': float(orders_last_30_days.aggregate(total=Sum('total'))['total'] or 0),
                    'average_order_value': float(average_order_value),
                    'average_paid_order_value': float(average_paid_order_value),
                    'paid_orders': paid_orders.count(),
                    'delivered_orders': all_orders.filter(status=Order.Status.DELIVERED).count(),
                    'unique_buyers': unique_buyers,
                    'repeat_buyers': repeat_buyers,
                    'top_payment_method': {
                        'label': payment_labels.get(top_payment_method['payment_method'], top_payment_method['payment_method']),
                        'count': top_payment_method['count'],
                        'amount': float(top_payment_method['amount'] or 0),
                    } if top_payment_method else None,
                    'payment_breakdown': [
                        {
                            'label': payment_labels.get(item['payment_method'], item['payment_method']),
                            'count': item['count'],
                            'amount': float(item['amount'] or 0),
                        }
                        for item in payment_breakdown
                    ],
                    'status_breakdown': [
                        {
                            'label': status_labels.get(item['status'], item['status']),
                            'count': item['count'],
                        }
                        for item in status_breakdown
                    ],
                },
            }

        return cached_response(
            'admin-overview',
            request,
            USER_CACHE_TTL,
            build_payload,
            user_scoped=True,
        )


class AdminResetTestDataView(APIView):
    permission_classes = [permissions.IsAuthenticated, permissions.IsAdminUser]

    def post(self, request):
        protected_admin_id = request.user.id

        with transaction.atomic():
            deleted_counts = {
                'deliveries': Delivery.objects.count(),
                'orders': Order.objects.count(),
                'measurements': MeasurementProfile.objects.count(),
                'fabrics': Fabric.objects.count(),
                'designs': Design.objects.count(),
                'tailors': User.objects.filter(role=User.Role.TAILOR).count(),
                'drivers': User.objects.filter(role=User.Role.DRIVER).count(),
                'customers': User.objects.filter(role=User.Role.CUSTOMER).count(),
            }

            Delivery.objects.all().delete()
            Order.objects.all().delete()
            MeasurementProfile.objects.all().delete()
            Fabric.objects.all().delete()
            Design.objects.all().delete()
            User.objects.exclude(id=protected_admin_id).delete()

        return Response(
            {
                'detail': 'All test data deleted successfully. The current admin account was kept.',
                'deleted': deleted_counts,
            },
            status=status.HTTP_200_OK,
        )


class AdminUsersListView(APIView):
    permission_classes = [permissions.IsAuthenticated, permissions.IsAdminUser]

    def get(self, request):
        """Get list of all users with their latest session information"""
        try:
            # Get all users
            users = User.objects.all().order_by('-id')
            
            # Prepare user data with session information
            users_data = []
            for user in users:
                try:
                    # Get the most recent session for this user
                    latest_session = UserSession.objects.filter(user=user).order_by('-login_time').first()
                    
                    if latest_session:
                        users_data.append({
                            'id': user.id,
                            'full_name': user.full_name,
                            'email': user.email,
                            'role': user.role,
                            'login_time': latest_session.login_time,
                            'logout_time': latest_session.logout_time,
                        })
                    else:
                        users_data.append({
                            'id': user.id,
                            'full_name': user.full_name,
                            'email': user.email,
                            'role': user.role,
                            'login_time': None,
                            'logout_time': None,
                        })
                except Exception:
                    # If UserSession table doesn't exist, return basic user info
                    users_data.append({
                        'id': user.id,
                        'full_name': user.full_name,
                        'email': user.email,
                        'role': user.role,
                        'login_time': None,
                        'logout_time': None,
                    })
            
            return Response(users_data)
        except Exception as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class AdminTailorViewSet(viewsets.ModelViewSet):
    serializer_class = AdminTailorDetailSerializer
    permission_classes = [permissions.IsAuthenticated, permissions.IsAdminUser]
    lookup_field = 'user_id'
    lookup_url_kwarg = 'pk'

    def get_queryset(self):
        return (
            TailorProfile.objects.select_related('user')
            .annotate(active_orders=Count('user__tailor_orders'))
            .prefetch_related(
                Prefetch(
                    'user__tailor_orders',
                    queryset=Order.objects.select_related('customer')
                    .only(
                        'id',
                        'status',
                        'payment_method',
                        'payment_status',
                        'total',
                        'created_at',
                        'customer__full_name',
                        'customer_phone',
                    )
                    .order_by('-created_at'),
                )
            )
            .only(
                'user__id',
                'user__full_name',
                'user__email',
                'user__phone',
                'user__address',
                'rating',
                'specialty',
                'location',
                'eta',
                'image',
                'about',
                'service_price',
                'is_featured',
                'is_active',
                'national_id',
                'shop_name',
                'bank_name',
                'account_title',
                'account_number',
                'iban',
            )
        )

    def list(self, request, *args, **kwargs):
        return cached_response(
            'admin-tailors',
            request,
            USER_CACHE_TTL,
            lambda: self.get_serializer(self.get_queryset(), many=True).data,
            user_scoped=True,
        )


class AdminDriverViewSet(viewsets.ModelViewSet):
    serializer_class = AdminDriverDetailSerializer
    permission_classes = [permissions.IsAuthenticated, permissions.IsAdminUser]
    lookup_field = 'user_id'
    lookup_url_kwarg = 'pk'

    def get_serializer_class(self):
        if self.request.query_params.get('summary') == '1':
            return AdminDriverAssignmentSerializer
        return AdminDriverDetailSerializer

    def get_queryset(self):
        if self.request.query_params.get('summary') == '1':
            return (
                DriverProfile.objects.select_related('user')
                .only(
                    'user__id',
                    'user__full_name',
                    'user__phone',
                    'vehicle_type',
                    'is_available',
                )
                .order_by('user__full_name')
            )

        return (
            DriverProfile.objects.select_related('user')
            .annotate(active_deliveries=Count('user__deliveries'))
            .prefetch_related(
                Prefetch(
                    'user__deliveries',
                    queryset=Delivery.objects.select_related('order', 'order__customer', 'order__tailor')
                    .only(
                        'id',
                        'order__id',
                        'order__customer__full_name',
                        'order__customer_phone',
                        'order__tailor__full_name',
                        'order__tailor__phone',
                        'delivery_address',
                        'status',
                        'assigned_date',
                    )
                    .order_by('-assigned_date', '-id'),
                )
            )
            .only(
                'user__id',
                'user__full_name',
                'user__email',
                'user__phone',
                'user__address',
                'vehicle_type',
                'vehicle_number',
                'license_number',
                'is_available',
                'national_id',
                'bank_name',
                'account_title',
                'account_number',
                'iban',
            )
        )

    def list(self, request, *args, **kwargs):
        return cached_response(
            'admin-drivers',
            request,
            USER_CACHE_TTL,
            lambda: self.get_serializer(self.get_queryset(), many=True).data,
            user_scoped=True,
        )


class AdminFabricViewSet(viewsets.ModelViewSet):
    serializer_class = FabricSerializer
    permission_classes = [permissions.IsAuthenticated, permissions.IsAdminUser]
    parser_classes = IMAGE_UPLOAD_PARSER_CLASSES

    def get_queryset(self):
        return Fabric.objects.only(
            'id',
            'material',
            'color',
            'price',
            'image',
            'images',
            'shop',
            'description',
            'uploaded_by_id',
            'is_active',
        ).order_by('-created_at')

    def list(self, request, *args, **kwargs):
        return cached_response(
            'admin-fabrics',
            request,
            USER_CACHE_TTL,
            lambda: self.get_serializer(self.get_queryset(), many=True).data,
            user_scoped=True,
        )

    def perform_create(self, serializer):
        serializer.save()
        invalidate_api_cache()

    def perform_update(self, serializer):
        serializer.save()
        invalidate_api_cache()

    def perform_destroy(self, instance):
        instance.delete()
        invalidate_api_cache()


class AdminDesignViewSet(viewsets.ModelViewSet):
    serializer_class = DesignSerializer
    permission_classes = [permissions.IsAuthenticated, permissions.IsAdminUser]
    parser_classes = IMAGE_UPLOAD_PARSER_CLASSES

    def get_queryset(self):
        return Design.objects.select_related('uploaded_by', 'uploaded_by__tailor_profile').only(
            'id',
            'title',
            'category',
            'image',
            'images',
            'description',
            'compatible_fabrics',
            'designer',
            'uploaded_by_id',
            'uploaded_by__role',
            'uploaded_by__full_name',
            'uploaded_by__tailor_profile__shop_name',
            'base_price',
            'is_active',
            'created_at',
        ).order_by('-created_at')

    def list(self, request, *args, **kwargs):
        return cached_response(
            'admin-designs',
            request,
            USER_CACHE_TTL,
            lambda: self.get_serializer(self.get_queryset(), many=True).data,
            user_scoped=True,
        )

    def perform_create(self, serializer):
        serializer.save()
        invalidate_api_cache()

    def perform_update(self, serializer):
        serializer.save()
        invalidate_api_cache()

    def perform_destroy(self, instance):
        instance.delete()
        invalidate_api_cache()


class AdminOrderListView(generics.ListAPIView):
    serializer_class = AdminOrderListSerializer
    permission_classes = [permissions.IsAuthenticated, permissions.IsAdminUser]

    def get_queryset(self):
        return (
            Order.objects.select_related('customer', 'tailor', 'design', 'fabric', 'delivery', 'delivery__driver')
            .only(
                'id',
                'customer__full_name',
                'customer_phone',
                'tailor__full_name',
                'tailor__phone',
                'design__title',
                'design__image',
                'design__images',
                'fabric__material',
                'fabric__color',
                'fabric__image',
                'fabric__images',
                'status',
                'payment_method',
                'payment_status',
                'stripe_checkout_session_id',
                'stripe_payment_intent_id',
                'stripe_refund_id',
                'platform_fee',
                'tailor_payout',
                'subtotal',
                'delivery_fee',
                'total',
                'delivery_address',
                'estimated_completion_date',
                'paid_at',
                'refunded_at',
                'notes',
                'created_at',
                'delivery__driver__full_name',
                'delivery__driver_id',
            )
            .order_by('-created_at')
        )

    def list(self, request, *args, **kwargs):
        return cached_response(
            'admin-orders',
            request,
            USER_CACHE_TTL,
            lambda: self.get_serializer(self.get_queryset(), many=True).data,
            user_scoped=True,
        )


class AdminOrderDetailUpdateView(generics.RetrieveUpdateAPIView):
    queryset = Order.objects.select_related('customer', 'tailor', 'design', 'fabric', 'measurement', 'delivery', 'delivery__driver').all()
    serializer_class = AdminOrderDetailSerializer
    permission_classes = [permissions.IsAuthenticated, permissions.IsAdminUser]

    def patch(self, request, *args, **kwargs):
        order = self.get_object()
        new_status = str(request.data.get('status', '')).strip()

        if not new_status:
            return Response({'detail': 'Status is required.'}, status=status.HTTP_400_BAD_REQUEST)

        if new_status not in Order.Status.values:
            return Response({'detail': 'Invalid order status.'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            update_order_status_with_rules(order, new_status, ADMIN_STATUS_FLOW, actor_label='Admin')
        except ValueError as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        refreshed_order = self.get_queryset().get(pk=order.pk)
        invalidate_api_cache()
        return Response(AdminOrderDetailSerializer(refreshed_order).data)


class AdminOrderRefundView(APIView):
    permission_classes = [permissions.IsAuthenticated, permissions.IsAdminUser]

    def post(self, request, order_id):
        order = get_object_or_404(Order.objects.select_related('customer', 'tailor'), pk=order_id)

        if order.payment_status != Order.PaymentStatus.PAID:
            return Response({'detail': 'Only paid orders can be refunded.'}, status=status.HTTP_400_BAD_REQUEST)
        if not order.stripe_payment_intent_id:
            return Response({'detail': 'This order does not have a Stripe payment intent to refund.'}, status=status.HTTP_400_BAD_REQUEST)

        amount_value = request.data.get('amount')
        refund_kwargs = {
            'payment_intent': order.stripe_payment_intent_id,
            'metadata': {
                'order_id': str(order.id),
                'customer_id': str(order.customer_id),
                'tailor_id': str(order.tailor_id),
                'reason': str(request.data.get('reason') or 'admin_refund')[:500],
            },
        }
        if amount_value not in (None, ''):
            amount = stripe_amount_from_decimal(amount_value)
            if amount <= 0:
                return Response({'detail': 'Refund amount must be greater than zero.'}, status=status.HTTP_400_BAD_REQUEST)
            if amount > stripe_amount_from_decimal(order.total):
                return Response({'detail': 'Refund amount cannot exceed the order total.'}, status=status.HTTP_400_BAD_REQUEST)
            refund_kwargs['amount'] = amount

        try:
            stripe = get_stripe_module()
            refund = stripe.Refund.create(**refund_kwargs)
        except RuntimeError as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        except Exception as exc:
            logger.exception('Stripe refund failed for order %s.', order.id)
            return Response({'detail': f'Could not refund Stripe payment: {exc}'}, status=status.HTTP_502_BAD_GATEWAY)

        if getattr(refund, 'status', '') in {'succeeded', 'pending', 'requires_action'}:
            apply_refunded_state(order, refund_id=str(getattr(refund, 'id', '') or ''))

        refreshed_order = Order.objects.select_related('customer', 'tailor', 'design', 'fabric', 'measurement', 'delivery', 'delivery__driver').get(pk=order.pk)
        return Response(AdminOrderDetailSerializer(refreshed_order).data)


class AdminAssignDriverView(APIView):
    permission_classes = [permissions.IsAuthenticated, permissions.IsAdminUser]

    def post(self, request, order_id):
        serializer = AdminAssignDriverSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        order = get_object_or_404(Order, pk=order_id)
        driver = get_object_or_404(User, pk=serializer.validated_data['driver_id'], role=User.Role.DRIVER)
        pickup_address = order.tailor.address
        if not pickup_address and hasattr(order.tailor, 'tailor_profile'):
            pickup_address = order.tailor.tailor_profile.location
        delivery, _ = Delivery.objects.get_or_create(
            order=order,
            defaults={
                'pickup_address': pickup_address,
                'delivery_address': order.delivery_address,
            },
        )
        delivery.driver = driver
        delivery.status = Delivery.Status.ASSIGNED
        delivery.assigned_date = timezone.localdate()
        delivery.admin_notes = serializer.validated_data.get('admin_notes', '')
        delivery.pickup_address = delivery.pickup_address or pickup_address
        delivery.delivery_address = order.delivery_address
        delivery.save()

        order.status = Order.Status.OUT_FOR_DELIVERY if order.status == Order.Status.READY else order.status
        order.save(update_fields=['status', 'updated_at'])
        invalidate_api_cache()

        return Response(DeliverySerializer(delivery).data)


@api_view(['POST'])
@permission_classes([permissions.IsAuthenticated])
@api_view(['POST'])
@permission_classes([permissions.IsAuthenticated])
def logout_view(request):
    # Mark the most recent session as logged out (gracefully handle if table doesn't exist)
    try:
        current_session = UserSession.objects.filter(
            user=request.user,
            logout_time__isnull=True
        ).order_by('-login_time').first()
        
        if current_session:
            current_session.logout_time = timezone.now()
            current_session.save(update_fields=['logout_time'])
    except Exception:
        # UserSession table may not exist yet on production - that's OK
        pass
    
    Token.objects.filter(user=request.user).delete()
    return Response({'detail': 'Logged out successfully.'})
