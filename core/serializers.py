from decimal import Decimal

from django.contrib.auth import authenticate
from django.core.exceptions import ObjectDoesNotExist
from django.db.models import Q
from rest_framework import serializers
from rest_framework.authtoken.models import Token

from .models import Delivery, Design, DriverProfile, Fabric, MeasurementProfile, Order, TailorProfile, User


class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ['id', 'email', 'full_name', 'phone', 'role', 'address', 'is_staff']


class SignupSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, min_length=8)

    class Meta:
        model = User
        fields = ['id', 'email', 'full_name', 'phone', 'password', 'role', 'address']
        extra_kwargs = {
            'email': {'validators': []},
        }

    def validate_email(self, value):
        email = User.objects.normalize_email(value).lower().strip()
        if User.objects.filter(email__iexact=email).exists():
            raise serializers.ValidationError('An account with this email already exists.')
        return email

    def validate_full_name(self, value):
        full_name = value.strip()
        if not full_name:
            raise serializers.ValidationError('Full name is required.')
        return full_name

    def validate_role(self, value):
        if value == User.Role.ADMIN:
            raise serializers.ValidationError('Admin accounts cannot be created from signup.')
        return value

    def create(self, validated_data):
        password = validated_data.pop('password')
        user = User.objects.create_user(password=password, **validated_data)
        if user.role == User.Role.TAILOR:
            TailorProfile.objects.get_or_create(user=user)
        elif user.role == User.Role.DRIVER:
            DriverProfile.objects.get_or_create(user=user)
        return user


class LoginSerializer(serializers.Serializer):
    email = serializers.EmailField(required=False)
    username = serializers.CharField(required=False)
    password = serializers.CharField(write_only=True)

    def validate(self, attrs):
        username = (attrs.get('username') or '').strip()
        email = (attrs.get('email') or '').strip()
        password = attrs['password']

        if username:
            user = User.objects.filter(username__iexact=username).first()
            if not user:
                raise serializers.ValidationError('Invalid username or password.')
            if not user.is_active or not user.check_password(password):
                raise serializers.ValidationError('Invalid username or password.')
            attrs['user'] = user
            return attrs

        if not email:
            raise serializers.ValidationError('Username or email is required.')

        user_obj = User.objects.filter(email__iexact=email).first()
        if user_obj and user_obj.is_staff:
            raise serializers.ValidationError('Admin accounts must log in with username.')

        auth_username = user_obj.email if user_obj else email
        user = authenticate(username=auth_username, password=password)
        if not user:
            raise serializers.ValidationError('Invalid username/email or password.')
        attrs['user'] = user
        return attrs


class TailorProfileSerializer(serializers.ModelSerializer):
    id = serializers.IntegerField(source='user.id', read_only=True)
    name = serializers.CharField(source='user.full_name', read_only=True)
    email = serializers.CharField(source='user.email', read_only=True)
    phone = serializers.CharField(source='user.phone', read_only=True)
    address = serializers.CharField(source='user.address', read_only=True)

    class Meta:
        model = TailorProfile
        fields = [
            'id',
            'name',
            'email',
            'phone',
            'address',
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
        ]


class TailorShopSetupSerializer(serializers.Serializer):
    full_name = serializers.CharField(required=False, allow_blank=False, max_length=255)
    phone = serializers.CharField(required=False, allow_blank=True, max_length=30)
    address = serializers.CharField(required=False, allow_blank=True)
    shop_name = serializers.CharField(required=False, allow_blank=False, max_length=255)
    image = serializers.CharField(required=False, allow_blank=True)
    specialty = serializers.CharField(required=False, allow_blank=True, max_length=255)
    location = serializers.CharField(required=False, allow_blank=True, max_length=255)
    eta = serializers.CharField(required=False, allow_blank=True, max_length=100)
    about = serializers.CharField(required=False, allow_blank=True)
    bank_name = serializers.CharField(required=False, allow_blank=True, max_length=255)
    account_title = serializers.CharField(required=False, allow_blank=True, max_length=255)
    account_number = serializers.CharField(required=False, allow_blank=True, max_length=100)
    iban = serializers.CharField(required=False, allow_blank=True, max_length=100)

    def update(self, instance, validated_data):
        user = instance.user
        user_fields = ('full_name', 'phone', 'address')
        profile_fields = ('shop_name', 'image', 'specialty', 'location', 'eta', 'about', 'bank_name', 'account_title', 'account_number', 'iban')

        for field in user_fields:
            if field in validated_data:
                setattr(user, field, validated_data[field])
        user.save(update_fields=[field for field in user_fields if field in validated_data] or None)

        for field in profile_fields:
            if field in validated_data:
                setattr(instance, field, validated_data[field])
        instance.save(update_fields=[field for field in profile_fields if field in validated_data] or None)
        return instance


class DriverProfileSerializer(serializers.ModelSerializer):
    id = serializers.IntegerField(source='user.id', read_only=True)
    name = serializers.CharField(source='user.full_name', read_only=True)
    email = serializers.CharField(source='user.email', read_only=True)
    phone = serializers.CharField(source='user.phone', read_only=True)
    address = serializers.CharField(source='user.address', read_only=True)

    class Meta:
        model = DriverProfile
        fields = [
            'id',
            'name',
            'email',
            'phone',
            'address',
            'vehicle_type',
            'vehicle_number',
            'license_number',
            'is_available',
            'national_id',
            'bank_name',
            'account_title',
            'account_number',
            'iban',
        ]


class DriverProfileUpdateSerializer(serializers.Serializer):
    full_name = serializers.CharField(required=False, allow_blank=False, max_length=255)
    phone = serializers.CharField(required=False, allow_blank=True, max_length=30)
    address = serializers.CharField(required=False, allow_blank=True)
    vehicle_type = serializers.CharField(required=False, allow_blank=True, max_length=120)
    vehicle_number = serializers.CharField(required=False, allow_blank=True, max_length=50)
    license_number = serializers.CharField(required=False, allow_blank=True, max_length=100)
    is_available = serializers.BooleanField(required=False)
    bank_name = serializers.CharField(required=False, allow_blank=True, max_length=255)
    account_title = serializers.CharField(required=False, allow_blank=True, max_length=255)
    account_number = serializers.CharField(required=False, allow_blank=True, max_length=100)
    iban = serializers.CharField(required=False, allow_blank=True, max_length=100)

    def update(self, instance, validated_data):
        user = instance.user
        user_fields = ('full_name', 'phone', 'address')
        profile_fields = ('vehicle_type', 'vehicle_number', 'license_number', 'is_available', 'bank_name', 'account_title', 'account_number', 'iban')

        for field in user_fields:
            if field in validated_data:
                setattr(user, field, validated_data[field])
        user.save(update_fields=[field for field in user_fields if field in validated_data] or None)

        for field in profile_fields:
            if field in validated_data:
                setattr(instance, field, validated_data[field])
        instance.save(update_fields=[field for field in profile_fields if field in validated_data] or None)
        return instance


class FabricSerializer(serializers.ModelSerializer):
    images = serializers.ListField(child=serializers.CharField(), required=False, allow_empty=True)
    uploaded_by = serializers.IntegerField(source='uploaded_by_id', read_only=True)

    class Meta:
        model = Fabric
        fields = ['id', 'material', 'color', 'price', 'image', 'images', 'shop', 'description', 'uploaded_by', 'is_active']
        read_only_fields = ['uploaded_by']

    def _build_normalized_data(self, validated_data, user):
        normalized_data = dict(validated_data)

        material = normalized_data.get('material', '')
        color = normalized_data.get('color', '')
        shop = normalized_data.get('shop', '')
        description = normalized_data.get('description', '')
        image = normalized_data.get('image', '')
        images = normalized_data.get('images', [])

        normalized_data['material'] = material.strip()
        normalized_data['color'] = color.strip()
        normalized_data['shop'] = shop.strip()
        normalized_data['description'] = description.strip()
        normalized_data['image'] = image.strip()
        normalized_data['images'] = [str(item).strip() for item in images if str(item).strip()]

        if normalized_data['images'] and not normalized_data['image']:
            normalized_data['image'] = normalized_data['images'][0]

        if user and getattr(user, 'is_authenticated', False):
            normalized_data['uploaded_by'] = user
            if getattr(user, 'role', None) == User.Role.TAILOR and not normalized_data.get('shop'):
                tailor_profile = getattr(user, 'tailor_profile', None)
                normalized_data['shop'] = getattr(tailor_profile, 'shop_name', '') or getattr(user, 'full_name', '')

        return normalized_data

    def create(self, validated_data):
        request = self.context.get('request')
        user = getattr(request, 'user', None)
        normalized_data = self._build_normalized_data(validated_data, user)

        duplicate_query = Fabric.objects.filter(
            material__iexact=normalized_data['material'],
            color__iexact=normalized_data['color'],
            price=normalized_data['price'],
            image=normalized_data['image'],
            images=normalized_data['images'],
            description=normalized_data['description'],
            is_active=normalized_data.get('is_active', True),
        )

        if normalized_data.get('uploaded_by'):
            duplicate_query = duplicate_query.filter(uploaded_by=normalized_data['uploaded_by'])
        else:
            duplicate_query = duplicate_query.filter(uploaded_by__isnull=True)

        shop_value = normalized_data.get('shop', '')
        if shop_value:
            duplicate_query = duplicate_query.filter(shop__iexact=shop_value)
        else:
            duplicate_query = duplicate_query.filter(Q(shop='') | Q(shop__isnull=True))

        existing_fabric = duplicate_query.order_by('-id').first()
        if existing_fabric:
            return existing_fabric

        return super().create(normalized_data)

    def update(self, instance, validated_data):
        request = self.context.get('request')
        user = getattr(request, 'user', None)
        normalized_data = self._build_normalized_data(validated_data, user)
        return super().update(instance, normalized_data)


class DesignSerializer(serializers.ModelSerializer):
    images = serializers.ListField(child=serializers.CharField(), required=False, allow_empty=True)

    class Meta:
        model = Design
        fields = [
            'id',
            'title',
            'category',
            'image',
            'images',
            'description',
            'compatible_fabrics',
            'designer',
            'uploaded_by',
            'base_price',
            'is_active',
            'created_at',
        ]
        read_only_fields = ['uploaded_by', 'created_at']

    def create(self, validated_data):
        images = validated_data.pop('images', [])
        request = self.context.get('request')
        user = getattr(request, 'user', None)

        if images and not validated_data.get('image'):
            validated_data['image'] = images[0]

        if user and getattr(user, 'is_authenticated', False):
            validated_data['uploaded_by'] = user
            if getattr(user, 'role', None) == User.Role.TAILOR and not validated_data.get('designer'):
                validated_data['designer'] = getattr(user, 'full_name', '')

        validated_data['images'] = images
        return super().create(validated_data)

    def update(self, instance, validated_data):
        images = validated_data.pop('images', None)
        if images is not None:
            validated_data['images'] = images
            if images and not validated_data.get('image'):
                validated_data['image'] = images[0]
        return super().update(instance, validated_data)


class MeasurementSerializer(serializers.ModelSerializer):
    class Meta:
        model = MeasurementProfile
        fields = [
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
        ]
        read_only_fields = ['id', 'created_at']

    def create(self, validated_data):
        user = self.context['request'].user
        if validated_data.get('is_default', False):
            MeasurementProfile.objects.filter(customer=user, is_default=True).update(is_default=False)
        elif not MeasurementProfile.objects.filter(customer=user).exists():
            validated_data['is_default'] = True
        return MeasurementProfile.objects.create(customer=user, **validated_data)


class DeliverySerializer(serializers.ModelSerializer):
    order_id = serializers.IntegerField(source='order.id', read_only=True)
    customer_name = serializers.CharField(source='order.customer.full_name', read_only=True)
    customer_phone = serializers.CharField(source='order.customer_phone', read_only=True)
    tailor_name = serializers.CharField(source='order.tailor.full_name', read_only=True)
    tailor_phone = serializers.CharField(source='order.tailor.phone', read_only=True)
    driver_name = serializers.CharField(source='driver.full_name', read_only=True)

    class Meta:
        model = Delivery
        fields = [
            'id',
            'order_id',
            'customer_name',
            'customer_phone',
            'pickup_address',
            'delivery_address',
            'tailor_name',
            'tailor_phone',
            'driver',
            'driver_name',
            'status',
            'assigned_date',
            'pickup_time',
            'delivery_time',
            'admin_notes',
        ]


class AdminTailorOrderSummarySerializer(serializers.ModelSerializer):
    customer_name = serializers.CharField(source='customer.full_name', read_only=True)
    customer_phone = serializers.CharField(read_only=True)

    class Meta:
        model = Order
        fields = ['id', 'customer_name', 'customer_phone', 'status', 'payment_method', 'payment_status', 'total', 'created_at']


class AdminDriverDeliverySummarySerializer(serializers.ModelSerializer):
    order_id = serializers.IntegerField(source='order.id', read_only=True)
    customer_name = serializers.CharField(source='order.customer.full_name', read_only=True)
    customer_phone = serializers.CharField(source='order.customer_phone', read_only=True)
    tailor_name = serializers.CharField(source='order.tailor.full_name', read_only=True)
    tailor_phone = serializers.CharField(source='order.tailor.phone', read_only=True)

    class Meta:
        model = Delivery
        fields = [
            'id',
            'order_id',
            'customer_name',
            'customer_phone',
            'tailor_name',
            'tailor_phone',
            'delivery_address',
            'status',
            'assigned_date',
        ]


class OrderSerializer(serializers.ModelSerializer):
    tailor_name = serializers.CharField(source='tailor.full_name', read_only=True)
    tailor_phone = serializers.CharField(source='tailor.phone', read_only=True)
    design_name = serializers.CharField(source='design.title', read_only=True)
    design_image = serializers.CharField(source='design.image', read_only=True)
    design_images = serializers.ListField(source='design.images', child=serializers.CharField(), read_only=True)
    fabric_name = serializers.CharField(source='fabric.material', read_only=True)
    fabric_color = serializers.CharField(source='fabric.color', read_only=True)
    fabric_image = serializers.CharField(source='fabric.image', read_only=True)
    fabric_images = serializers.ListField(source='fabric.images', child=serializers.CharField(), read_only=True)
    customer_name = serializers.CharField(source='customer.full_name', read_only=True)
    customer_email = serializers.CharField(source='customer.email', read_only=True)
    measurement = serializers.SerializerMethodField()
    delivery = serializers.SerializerMethodField()

    class Meta:
        model = Order
        fields = [
            'id',
            'customer',
            'customer_name',
            'customer_email',
            'customer_phone',
            'tailor',
            'tailor_name',
            'tailor_phone',
            'design',
            'design_name',
            'design_image',
            'design_images',
            'fabric',
            'fabric_name',
            'fabric_color',
            'fabric_image',
            'fabric_images',
            'measurement',
            'garment_type',
            'notes',
            'status',
            'payment_method',
            'payment_status',
            'subtotal',
            'delivery_fee',
            'total',
            'delivery_address',
            'created_at',
            'updated_at',
            'delivery',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at', 'customer']

    def get_delivery(self, obj):
        try:
            delivery = obj.delivery
        except ObjectDoesNotExist:
            return None
        return DeliverySerializer(delivery).data

    def get_measurement(self, obj):
        measurement = obj.measurement
        if measurement is None:
            measurement = (
                MeasurementProfile.objects.filter(customer=obj.customer, is_default=True).first()
                or MeasurementProfile.objects.filter(customer=obj.customer).order_by('-created_at').first()
            )
        return MeasurementSerializer(measurement).data if measurement else None

    def create(self, validated_data):
        user = self.context['request'].user
        if not validated_data.get('measurement'):
            validated_data['measurement'] = (
                MeasurementProfile.objects.filter(customer=user, is_default=True).first()
                or MeasurementProfile.objects.filter(customer=user).order_by('-created_at').first()
            )

        if not validated_data.get('tailor'):
            featured_tailor = User.objects.filter(
                role=User.Role.TAILOR,
                tailor_profile__is_featured=True,
                tailor_profile__is_active=True,
            ).first()
            validated_data['tailor'] = featured_tailor or User.objects.filter(role=User.Role.TAILOR).first()

        validated_data.setdefault('customer_phone', user.phone)
        design = validated_data.get('design')
        fabric = validated_data.get('fabric')
        tailor = validated_data.get('tailor')
        subtotal = Decimal('0.00')
        if tailor and hasattr(tailor, 'tailor_profile'):
            subtotal += tailor.tailor_profile.service_price
        if design:
            subtotal += design.base_price
        if fabric:
            subtotal += fabric.price
        delivery_fee = validated_data.get('delivery_fee', Decimal('0.00'))
        validated_data.setdefault('subtotal', subtotal)
        validated_data.setdefault('total', subtotal + delivery_fee)
        order = Order.objects.create(customer=user, **validated_data)
        pickup_address = ''
        if tailor:
            pickup_address = tailor.address
            if not pickup_address and hasattr(tailor, 'tailor_profile'):
                pickup_address = tailor.tailor_profile.location
        Delivery.objects.create(
            order=order,
            pickup_address=pickup_address,
            delivery_address=order.delivery_address,
        )
        return order


class DashboardSerializer(serializers.Serializer):
    top_tailors = TailorProfileSerializer(many=True)
    fabrics = FabricSerializer(many=True)
    measurements = MeasurementSerializer(many=True)
    recent_orders = OrderSerializer(many=True)
    designs = DesignSerializer(many=True)


class TailorShopCatalogSerializer(serializers.Serializer):
    tailor = TailorProfileSerializer()
    fabrics = FabricSerializer(many=True)
    designs = DesignSerializer(many=True)


class AdminTailorDetailSerializer(TailorProfileSerializer):
    active_orders = serializers.IntegerField(read_only=True)
    recent_orders = serializers.SerializerMethodField()

    class Meta(TailorProfileSerializer.Meta):
        fields = TailorProfileSerializer.Meta.fields + ['active_orders', 'recent_orders']

    def get_recent_orders(self, obj):
        orders = obj.user.tailor_orders.all().select_related('customer')[:5]
        return AdminTailorOrderSummarySerializer(orders, many=True).data


class AdminDriverDetailSerializer(DriverProfileSerializer):
    active_deliveries = serializers.IntegerField(read_only=True)
    recent_deliveries = serializers.SerializerMethodField()

    class Meta(DriverProfileSerializer.Meta):
        fields = DriverProfileSerializer.Meta.fields + ['active_deliveries', 'recent_deliveries']

    def get_recent_deliveries(self, obj):
        deliveries = obj.user.deliveries.all().select_related('order', 'order__customer', 'order__tailor')[:5]
        return AdminDriverDeliverySummarySerializer(deliveries, many=True).data


class AdminOrderDetailSerializer(OrderSerializer):
    assigned_driver_name = serializers.SerializerMethodField()
    assigned_driver_id = serializers.SerializerMethodField()

    class Meta(OrderSerializer.Meta):
        fields = OrderSerializer.Meta.fields + ['assigned_driver_name', 'assigned_driver_id']

    def get_assigned_driver_name(self, obj):
        try:
            delivery = obj.delivery
        except ObjectDoesNotExist:
            return None
        return delivery.driver.full_name if delivery.driver else None

    def get_assigned_driver_id(self, obj):
        try:
            delivery = obj.delivery
        except ObjectDoesNotExist:
            return None
        return delivery.driver.id if delivery.driver else None


class AdminAssignDriverSerializer(serializers.Serializer):
    driver_id = serializers.IntegerField()
    admin_notes = serializers.CharField(required=False, allow_blank=True)


class NotificationSerializer(serializers.Serializer):
    id = serializers.CharField()
    title = serializers.CharField()
    message = serializers.CharField()
    created_at = serializers.DateTimeField()


class InvoiceSerializer(serializers.Serializer):
    id = serializers.CharField()
    order_id = serializers.IntegerField()
    customer_name = serializers.CharField()
    tailor_name = serializers.CharField()
    total = serializers.DecimalField(max_digits=10, decimal_places=2)
    payment_method = serializers.CharField()
    payment_status = serializers.CharField()
    created_at = serializers.DateTimeField()


def build_auth_payload(user):
    token, _ = Token.objects.get_or_create(user=user)
    return {
        'token': token.key,
        'user': UserSerializer(user).data,
    }
