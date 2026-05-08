import hashlib
import json
from datetime import timedelta

from django.core.cache import cache
from django.db import transaction
from django.db.models import Avg, Count, Prefetch, Sum
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import generics, parsers, permissions, status, viewsets
from rest_framework.authtoken.models import Token
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Delivery, Design, DriverProfile, Fabric, MeasurementProfile, Order, TailorProfile, User, UserSession
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
    PublicTailorSerializer,
    SignupSerializer,
    TailorShopCatalogSerializer,
    TailorShopSetupSerializer,
    TailorOrderDetailSerializer,
    TailorOrderListSerializer,
    TailorProfileSerializer,
    UserSerializer,
    build_auth_payload,
)

CACHE_VERSION_KEY = 'api-cache-version'
PUBLIC_CACHE_TTL = 120
USER_CACHE_TTL = 60
MAX_CACHEABLE_PAYLOAD_BYTES = 262144
IMAGE_UPLOAD_PARSER_CLASSES = [parsers.JSONParser, parsers.FormParser, parsers.MultiPartParser]


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
        
        # Create a session record for tracking
        user_agent = request.META.get('HTTP_USER_AGENT', '')
        x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR', '')
        ip_address = x_forwarded_for.split(',')[0].strip() if x_forwarded_for else request.META.get('REMOTE_ADDR', '')
        
        UserSession.objects.create(
            user=user,
            ip_address=ip_address,
            user_agent=user_agent
        )
        
        return Response(build_auth_payload(user))


class ProfileView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        return Response(UserSerializer(request.user).data)

    def patch(self, request):
        user = request.user
        allowed_fields = ('full_name', 'phone', 'address')

        for field in allowed_fields:
            if field in request.data:
                setattr(user, field, str(request.data.get(field, '')).strip())

        user.save(update_fields=[field for field in allowed_fields if field in request.data] or None)
        invalidate_api_cache()
        return Response(UserSerializer(user).data)


class CustomerDashboardView(APIView):
    permission_classes = [permissions.IsAuthenticated, IsCustomer]

    def get(self, request):
        return cached_response(
            'customer-dashboard',
            request,
            USER_CACHE_TTL,
            lambda: DashboardSerializer(
                {
                    'top_tailors': TailorProfile.objects.filter(is_featured=True, is_active=True)
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
                    'fabrics': Fabric.objects.filter(is_active=True)
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
                        'total',
                        'created_at',
                        'tailor__full_name',
                        'design__title',
                    )[:10],
                    'designs': Design.objects.filter(is_active=True)
                    .only(
                        'id',
                        'title',
                        'category',
                        'image',
                        'images',
                        'description',
                        'compatible_fabrics',
                        'designer',
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
            TailorProfile.objects.filter(is_active=True)
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
        TailorProfile.objects.filter(is_active=True)
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
                TailorProfile.objects.filter(is_active=True)
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
                'fabrics': Fabric.objects.filter(uploaded_by_id=pk, is_active=True)
                .only('id', 'material', 'color', 'price', 'image', 'images', 'shop', 'description', 'is_active')
                .order_by('-created_at'),
                'designs': Design.objects.filter(uploaded_by_id=pk, is_active=True)
                .only(
                    'id',
                    'title',
                    'category',
                    'image',
                    'images',
                    'description',
                    'compatible_fabrics',
                    'designer',
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
    queryset = Fabric.objects.filter(is_active=True)
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
        Fabric.objects.filter(is_active=True)
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
    queryset = Design.objects.filter(is_active=True)
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
            .only(
                'id',
                'title',
                'category',
                'image',
                'images',
                'description',
                'compatible_fabrics',
                'designer',
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
        Design.objects.filter(is_active=True)
        .only(
            'id',
            'title',
            'category',
            'image',
            'images',
            'description',
            'compatible_fabrics',
            'designer',
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
        return Design.objects.filter(uploaded_by=self.request.user).order_by('-created_at')

    def list(self, request, *args, **kwargs):
        return cached_response(
            'tailor-designs',
            request,
            USER_CACHE_TTL,
            lambda: self.get_serializer(self.get_queryset(), many=True).data,
            user_scoped=True,
        )

    def perform_create(self, serializer):
        serializer.save()
        invalidate_api_cache()


class TailorDesignDetailView(generics.RetrieveDestroyAPIView):
    serializer_class = DesignSerializer
    permission_classes = [permissions.IsAuthenticated, IsTailor]

    def get_queryset(self):
        return Design.objects.filter(uploaded_by=self.request.user)

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

        if not new_status:
            return Response({'detail': 'Status is required.'}, status=status.HTTP_400_BAD_REQUEST)

        if new_status not in Order.Status.values:
            return Response({'detail': 'Invalid order status.'}, status=status.HTTP_400_BAD_REQUEST)

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
        return Design.objects.only(
            'id',
            'title',
            'category',
            'image',
            'images',
            'description',
            'compatible_fabrics',
            'designer',
            'uploaded_by_id',
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
                'subtotal',
                'delivery_fee',
                'total',
                'delivery_address',
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
    # Mark the most recent session as logged out
    current_session = UserSession.objects.filter(
        user=request.user,
        logout_time__isnull=True
    ).order_by('-login_time').first()
    
    if current_session:
        current_session.logout_time = timezone.now()
        current_session.save(update_fields=['logout_time'])
    
    Token.objects.filter(user=request.user).delete()
    return Response({'detail': 'Logged out successfully.'})
