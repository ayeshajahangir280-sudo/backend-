from django.db import transaction
from django.db.models import Prefetch
from django.db.models import Count
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import generics, permissions, status, viewsets
from rest_framework.authtoken.models import Token
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Delivery, Design, DriverProfile, Fabric, MeasurementProfile, Order, TailorProfile, User
from .permissions import IsCustomer, IsDriver, IsTailor
from .serializers import (
    AdminAssignDriverSerializer,
    AdminDriverDetailSerializer,
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
    SignupSerializer,
    TailorShopCatalogSerializer,
    TailorShopSetupSerializer,
    TailorOrderDetailSerializer,
    TailorProfileSerializer,
    UserSerializer,
    build_auth_payload,
)


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
        return Response(build_auth_payload(serializer.validated_data['user']))


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
        return Response(UserSerializer(user).data)


class CustomerDashboardView(APIView):
    permission_classes = [permissions.IsAuthenticated, IsCustomer]

    def get(self, request):
        payload = {
            'top_tailors': TailorProfile.objects.filter(is_featured=True, is_active=True).select_related('user')[:10],
            'fabrics': Fabric.objects.filter(is_active=True)[:10],
            'measurements': MeasurementProfile.objects.filter(customer=request.user)[:10],
            'recent_orders': Order.objects.filter(customer=request.user).select_related('customer', 'tailor', 'design', 'fabric', 'measurement', 'delivery', 'delivery__driver')[:10],
            'designs': Design.objects.filter(is_active=True)[:10],
        }
        return Response(DashboardSerializer(payload).data)


class TailorListView(generics.ListAPIView):
    queryset = TailorProfile.objects.filter(is_active=True).select_related('user')
    serializer_class = TailorProfileSerializer
    permission_classes = [permissions.AllowAny]

    def get_queryset(self):
        queryset = super().get_queryset()
        if self.request.query_params.get('top') == '1':
            queryset = queryset.filter(is_featured=True)
        return queryset


class TailorDetailView(generics.RetrieveAPIView):
    queryset = TailorProfile.objects.filter(is_active=True).select_related('user')
    serializer_class = TailorProfileSerializer
    permission_classes = [permissions.AllowAny]
    lookup_field = 'user_id'
    lookup_url_kwarg = 'pk'


class TailorShopCatalogView(APIView):
    permission_classes = [permissions.AllowAny]

    def get(self, request, pk):
        tailor = get_object_or_404(
            TailorProfile.objects.filter(is_active=True).select_related('user'),
            user_id=pk,
        )
        payload = {
            'tailor': tailor,
            'fabrics': Fabric.objects.filter(uploaded_by_id=pk, is_active=True).order_by('-created_at'),
            'designs': Design.objects.filter(uploaded_by_id=pk, is_active=True).order_by('-created_at'),
        }
        return Response(TailorShopCatalogSerializer(payload).data)


class TailorMeView(APIView):
    permission_classes = [permissions.IsAuthenticated, IsTailor]

    def get_profile(self, user):
        profile, _ = TailorProfile.objects.get_or_create(user=user)
        return profile

    def get(self, request):
        profile = self.get_profile(request.user)
        return Response(TailorProfileSerializer(profile).data)

    def patch(self, request):
        profile = self.get_profile(request.user)
        serializer = TailorShopSetupSerializer(profile, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        profile = serializer.save()
        return Response(TailorProfileSerializer(profile).data)


class DriverMeView(APIView):
    permission_classes = [permissions.IsAuthenticated, IsDriver]

    def get_profile(self, user):
        profile, _ = DriverProfile.objects.get_or_create(user=user)
        return profile

    def get(self, request):
        profile = self.get_profile(request.user)
        return Response(DriverProfileSerializer(profile).data)

    def patch(self, request):
        profile = self.get_profile(request.user)
        serializer = DriverProfileUpdateSerializer(profile, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        profile = serializer.save()
        return Response(DriverProfileSerializer(profile).data)


class FabricListView(generics.ListCreateAPIView):
    queryset = Fabric.objects.filter(is_active=True)
    serializer_class = FabricSerializer
    permission_classes = [permissions.AllowAny]


class TailorFabricListCreateView(generics.ListCreateAPIView):
    serializer_class = FabricSerializer
    permission_classes = [permissions.IsAuthenticated, IsTailor]

    def get_queryset(self):
        return Fabric.objects.filter(uploaded_by=self.request.user).order_by('-created_at')

    def perform_create(self, serializer):
        serializer.save()


class TailorFabricDetailView(generics.RetrieveDestroyAPIView):
    serializer_class = FabricSerializer
    permission_classes = [permissions.IsAuthenticated, IsTailor]

    def get_queryset(self):
        return Fabric.objects.filter(uploaded_by=self.request.user)


class DesignListView(generics.ListCreateAPIView):
    queryset = Design.objects.filter(is_active=True)
    serializer_class = DesignSerializer
    permission_classes = [permissions.IsAuthenticatedOrReadOnly]

    def perform_create(self, serializer):
        serializer.save()


class DesignDetailView(generics.RetrieveAPIView):
    queryset = Design.objects.filter(is_active=True)
    serializer_class = DesignSerializer
    permission_classes = [permissions.AllowAny]


class TailorDesignListCreateView(generics.ListCreateAPIView):
    serializer_class = DesignSerializer
    permission_classes = [permissions.IsAuthenticated, IsTailor]

    def get_queryset(self):
        return Design.objects.filter(uploaded_by=self.request.user).order_by('-created_at')

    def perform_create(self, serializer):
        serializer.save()


class TailorDesignDetailView(generics.RetrieveDestroyAPIView):
    serializer_class = DesignSerializer
    permission_classes = [permissions.IsAuthenticated, IsTailor]

    def get_queryset(self):
        return Design.objects.filter(uploaded_by=self.request.user)


class MeasurementListCreateView(generics.ListCreateAPIView):
    serializer_class = MeasurementSerializer
    permission_classes = [permissions.IsAuthenticated, IsCustomer]

    def get_queryset(self):
        return MeasurementProfile.objects.filter(customer=self.request.user)


class MeasurementDetailView(generics.RetrieveUpdateDestroyAPIView):
    serializer_class = MeasurementSerializer
    permission_classes = [permissions.IsAuthenticated, IsCustomer]

    def get_queryset(self):
        return MeasurementProfile.objects.filter(customer=self.request.user)


class OrderListCreateView(generics.ListCreateAPIView):
    serializer_class = OrderSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        queryset = Order.objects.select_related('customer', 'tailor', 'design', 'fabric', 'measurement', 'delivery', 'delivery__driver')
        if user.role == User.Role.TAILOR:
            return queryset.filter(tailor=user)
        return queryset.filter(customer=user)


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


class TailorOrderListView(generics.ListAPIView):
    serializer_class = OrderSerializer
    permission_classes = [permissions.IsAuthenticated, IsTailor]

    def get_queryset(self):
        return Order.objects.filter(tailor=self.request.user).select_related('customer', 'tailor', 'design', 'fabric', 'measurement', 'delivery')


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
        return Response(OrderSerializer(refreshed_order).data)


class DriverDeliveryListView(generics.ListAPIView):
    serializer_class = DeliverySerializer
    permission_classes = [permissions.IsAuthenticated, IsDriver]

    def get_queryset(self):
        return Delivery.objects.filter(driver=self.request.user).select_related('order', 'order__customer', 'order__tailor', 'driver')


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


class NotificationListView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        orders = Order.objects.filter(customer=request.user).select_related('tailor')[:10]
        notifications = [
            {
                'id': f'order-{order.id}',
                'title': f'Order #{order.id} update',
                'message': f'Your order with {order.tailor.full_name} is currently {order.status}.',
                'created_at': order.updated_at,
            }
            for order in orders
        ]
        return Response(NotificationSerializer(notifications, many=True).data)


class InvoiceListView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        orders = Order.objects.filter(customer=request.user).select_related('tailor')[:20]
        invoices = [
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
            for order in orders
        ]
        return Response(InvoiceSerializer(invoices, many=True).data)


class InvoiceDetailView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, invoice_id):
        try:
            order_id = int(str(invoice_id).replace('INV-', ''))
        except ValueError:
            return Response({'detail': 'Invoice not found.'}, status=status.HTTP_404_NOT_FOUND)

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
        return Response(InvoiceSerializer(payload).data)


class AdminOverviewView(APIView):
    permission_classes = [permissions.IsAuthenticated, permissions.IsAdminUser]

    def get(self, request):
        return Response(
            {
                'counts': {
                    'customers': User.objects.filter(role=User.Role.CUSTOMER).count(),
                    'tailors': User.objects.filter(role=User.Role.TAILOR).count(),
                    'drivers': User.objects.filter(role=User.Role.DRIVER).count(),
                    'orders': Order.objects.count(),
                    'pending_assignments': Delivery.objects.filter(status=Delivery.Status.PENDING_ASSIGNMENT).count(),
                    'featured_tailors': TailorProfile.objects.filter(is_featured=True, is_active=True).count(),
                    'available_drivers': DriverProfile.objects.filter(is_available=True).count(),
                    'fabrics': Fabric.objects.count(),
                    'designs': Design.objects.count(),
                }
            }
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


class AdminTailorViewSet(viewsets.ModelViewSet):
    queryset = TailorProfile.objects.select_related('user').annotate(active_orders=Count('user__tailor_orders')).prefetch_related(
        Prefetch(
            'user__tailor_orders',
            queryset=Order.objects.select_related('customer').order_by('-created_at'),
        )
    )
    serializer_class = AdminTailorDetailSerializer
    permission_classes = [permissions.IsAuthenticated, permissions.IsAdminUser]
    lookup_field = 'user_id'
    lookup_url_kwarg = 'pk'


class AdminDriverViewSet(viewsets.ModelViewSet):
    queryset = DriverProfile.objects.select_related('user').annotate(active_deliveries=Count('user__deliveries')).prefetch_related(
        Prefetch(
            'user__deliveries',
            queryset=Delivery.objects.select_related('order', 'order__customer', 'order__tailor').order_by('-assigned_date', '-id'),
        )
    )
    serializer_class = AdminDriverDetailSerializer
    permission_classes = [permissions.IsAuthenticated, permissions.IsAdminUser]
    lookup_field = 'user_id'
    lookup_url_kwarg = 'pk'


class AdminFabricViewSet(viewsets.ModelViewSet):
    queryset = Fabric.objects.all()
    serializer_class = FabricSerializer
    permission_classes = [permissions.IsAuthenticated, permissions.IsAdminUser]


class AdminDesignViewSet(viewsets.ModelViewSet):
    queryset = Design.objects.all()
    serializer_class = DesignSerializer
    permission_classes = [permissions.IsAuthenticated, permissions.IsAdminUser]


class AdminOrderListView(generics.ListAPIView):
    queryset = Order.objects.select_related('customer', 'tailor', 'design', 'fabric', 'measurement', 'delivery', 'delivery__driver').all()
    serializer_class = AdminOrderListSerializer
    permission_classes = [permissions.IsAuthenticated, permissions.IsAdminUser]


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

        return Response(DeliverySerializer(delivery).data)


@api_view(['POST'])
@permission_classes([permissions.IsAuthenticated])
def logout_view(request):
    Token.objects.filter(user=request.user).delete()
    return Response({'detail': 'Logged out successfully.'})
