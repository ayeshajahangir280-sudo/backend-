import base64
import io
import re
import warnings
from decimal import Decimal

from django.contrib.auth import authenticate
from django.core.exceptions import ObjectDoesNotExist
from django.db.models import Q
from PIL import Image, ImageOps
from rest_framework import serializers
from rest_framework.authtoken.models import Token

from .media_storage import MediaStorageError, sync_image_references_to_cloudinary
from .models import Delivery, Design, DriverProfile, Fabric, MeasurementProfile, Order, TailorProfile, User


def is_inline_image(value):
    return isinstance(value, str) and value.strip().lower().startswith('data:')


INLINE_IMAGE_PATTERN = re.compile(r'^data:(image/[-+.\w]+);base64,(.+)$', re.IGNORECASE | re.DOTALL)
MAX_INLINE_IMAGE_DIMENSION = 1600
INLINE_IMAGE_JPEG_QUALITY = 60
MAX_INLINE_IMAGE_BYTES = 5 * 1024 * 1024
MAX_INLINE_IMAGE_PIXELS = 12_000_000
MAX_INLINE_IMAGE_COUNT = 6


def _build_inline_image_size_error(field_name):
    max_mb = MAX_INLINE_IMAGE_BYTES / (1024 * 1024)
    return serializers.ValidationError({
        field_name: f'Inline image is too large. Keep each image under {max_mb:.0f} MB.'
    })


def validate_inline_image_count(values, field_name):
    if values is not None and len(values) > MAX_INLINE_IMAGE_COUNT:
        raise serializers.ValidationError({
            field_name: f'You can upload up to {MAX_INLINE_IMAGE_COUNT} images at a time.'
        })


def normalize_image_references(values):
    normalized_values = []
    for value in values or []:
        normalized_value = str(value or '').strip()
        if normalized_value and normalized_value not in normalized_values:
            normalized_values.append(normalized_value)
    return normalized_values


def optimize_inline_image(value, *, field_name='image'):
    if not is_inline_image(value):
        return value

    match = INLINE_IMAGE_PATTERN.match(value.strip())
    if not match:
        return value

    try:
        encoded_image = match.group(2).strip()
        estimated_raw_bytes = (len(encoded_image) * 3) // 4
        if estimated_raw_bytes > MAX_INLINE_IMAGE_BYTES:
            raise _build_inline_image_size_error(field_name)

        raw_bytes = base64.b64decode(match.group(2), validate=True)
        if len(raw_bytes) > MAX_INLINE_IMAGE_BYTES:
            raise _build_inline_image_size_error(field_name)

        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw_bytes)) as image:
                width, height = image.size
                if width * height > MAX_INLINE_IMAGE_PIXELS:
                    raise serializers.ValidationError({
                        field_name: 'Inline image dimensions are too large. Resize the image and try again.'
                    })

                image = ImageOps.exif_transpose(image)
                if image.mode not in ('RGB', 'L'):
                    image = image.convert('RGB')
                elif image.mode == 'L':
                    image = image.convert('RGB')

                image.thumbnail((MAX_INLINE_IMAGE_DIMENSION, MAX_INLINE_IMAGE_DIMENSION), Image.Resampling.LANCZOS)

                output = io.BytesIO()
                image.save(
                    output,
                    format='JPEG',
                    quality=INLINE_IMAGE_JPEG_QUALITY,
                    optimize=True,
                )

        encoded = base64.b64encode(output.getvalue()).decode('ascii')
        return f'data:image/jpeg;base64,{encoded}'
    except serializers.ValidationError:
        raise
    except Image.DecompressionBombWarning:
        raise serializers.ValidationError({
            field_name: 'Inline image dimensions are too large. Resize the image and try again.'
        })
    except Exception:
        return value


def optimize_inline_images(values, *, field_name='images'):
    validate_inline_image_count(values, field_name)
    return [
        optimize_inline_image(str(value).strip(), field_name=field_name)
        for value in normalize_image_references(values)
    ]


def get_public_image(primary_image, image_list):
    primary_image = str(primary_image or '').strip()
    image_list = normalize_image_references(image_list)

    if primary_image and not is_inline_image(primary_image):
        return primary_image

    for image in image_list:
        if image and not is_inline_image(image):
            return image

    if primary_image:
        return primary_image

    for image in image_list:
        if image:
            return image
    return ''


def get_public_images(primary_image, image_list):
    primary_image = str(primary_image or '').strip()
    image_list = normalize_image_references(image_list)
    public_images = [image for image in image_list if image and not is_inline_image(image)]
    fallback_image = primary_image if primary_image and not is_inline_image(primary_image) else ''

    if fallback_image and fallback_image not in public_images:
        return [fallback_image, *public_images]
    if public_images:
        return public_images

    fallback_images = []
    for image in [primary_image, *image_list]:
        if image and image not in fallback_images:
            fallback_images.append(image)
    return fallback_images


def get_dashboard_image(primary_image, image_list):
    primary_image = str(primary_image or '').strip()
    image_list = normalize_image_references(image_list)

    if primary_image and not is_inline_image(primary_image):
        return primary_image

    for image in image_list:
        if image and not is_inline_image(image):
            return image

    return ''


def sync_cloudinary_images_or_raise(primary_image, image_list=None, *, folder, field_name='image'):
    try:
        return sync_image_references_to_cloudinary(primary_image, image_list or [], folder=folder)
    except MediaStorageError as exc:
        raise serializers.ValidationError({field_name: str(exc)}) from exc


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

    def to_representation(self, instance):
        representation = super().to_representation(instance)
        representation['image'] = get_public_image(representation.get('image', ''), [])
        return representation


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

        if 'image' in validated_data:
            optimized_image = optimize_inline_image(str(validated_data.get('image', '')).strip(), field_name='image')
            validated_data['image'], _ = sync_cloudinary_images_or_raise(
                optimized_image,
                [],
                folder='tailor-profiles',
                field_name='image',
            )

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
    images = serializers.ListField(child=serializers.CharField(allow_blank=True), required=False, allow_empty=True)
    uploaded_by = serializers.IntegerField(source='uploaded_by_id', read_only=True)

    class Meta:
        model = Fabric
        fields = ['id', 'material', 'color', 'price', 'image', 'images', 'shop', 'description', 'uploaded_by', 'is_active']
        read_only_fields = ['uploaded_by']

    def to_representation(self, instance):
        representation = super().to_representation(instance)
        public_images = get_public_images(representation.get('image'), representation.get('images'))
        representation['image'] = get_public_image(representation.get('image'), public_images)
        representation['images'] = public_images
        return representation

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
        normalized_data['image'] = optimize_inline_image(image.strip(), field_name='image')
        normalized_data['images'] = optimize_inline_images(images, field_name='images')

        if normalized_data['image'] and normalized_data['image'] not in normalized_data['images']:
            normalized_data['images'] = [normalized_data['image'], *normalized_data['images']]

        if normalized_data['images'] and not normalized_data['image']:
            normalized_data['image'] = normalized_data['images'][0]

        normalized_data['image'], normalized_data['images'] = sync_cloudinary_images_or_raise(
            normalized_data['image'],
            normalized_data['images'],
            folder='fabrics',
            field_name='images',
        )

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
    category = serializers.CharField(required=False, allow_blank=True)
    images = serializers.ListField(child=serializers.CharField(allow_blank=True), required=False, allow_empty=True)

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

    def to_representation(self, instance):
        representation = super().to_representation(instance)
        public_images = get_public_images(representation.get('image'), representation.get('images'))
        representation['image'] = get_public_image(representation.get('image'), public_images)
        representation['images'] = public_images
        return representation

    def create(self, validated_data):
        images = optimize_inline_images(validated_data.pop('images', []), field_name='images')
        request = self.context.get('request')
        user = getattr(request, 'user', None)

        validated_data['category'] = (validated_data.get('category') or '').strip() or 'Custom'
        validated_data['image'] = optimize_inline_image(str(validated_data.get('image', '')).strip(), field_name='image')

        if validated_data['image'] and validated_data['image'] not in images:
            images = [validated_data['image'], *images]

        if images and not validated_data.get('image'):
            validated_data['image'] = images[0]

        validated_data['image'], images = sync_cloudinary_images_or_raise(
            validated_data['image'],
            images,
            folder='designs',
            field_name='images',
        )

        if user and getattr(user, 'is_authenticated', False):
            validated_data['uploaded_by'] = user
            if getattr(user, 'role', None) == User.Role.TAILOR and not validated_data.get('designer'):
                validated_data['designer'] = getattr(user, 'full_name', '')

        validated_data['images'] = images
        return super().create(validated_data)

    def update(self, instance, validated_data):
        images = validated_data.pop('images', None)
        if 'category' in validated_data:
            validated_data['category'] = (validated_data.get('category') or '').strip() or instance.category or 'Custom'
        if 'image' in validated_data:
            validated_data['image'] = optimize_inline_image(str(validated_data.get('image', '')).strip(), field_name='image')
        if images is not None:
            validated_data['images'] = optimize_inline_images(images, field_name='images')
            if validated_data.get('image') and validated_data['image'] not in validated_data['images']:
                validated_data['images'] = [validated_data['image'], *validated_data['images']]
            if images and not validated_data.get('image'):
                validated_data['image'] = validated_data['images'][0]
        if 'image' in validated_data or images is not None:
            synced_image, synced_images = sync_cloudinary_images_or_raise(
                validated_data.get('image', instance.image),
                validated_data.get('images', instance.images),
                folder='designs',
                field_name='images' if images is not None else 'image',
            )
            validated_data['image'] = synced_image
            if images is not None or 'image' in validated_data:
                validated_data['images'] = synced_images
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
    measurement_id = serializers.PrimaryKeyRelatedField(
        queryset=MeasurementProfile.objects.all(),
        source='measurement',
        required=False,
        allow_null=True,
        write_only=True,
    )
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
            'measurement_id',
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

    def validate(self, attrs):
        request = self.context.get('request')
        user = getattr(request, 'user', None)
        measurement = attrs.get('measurement')
        tailor = attrs.get('tailor')

        if measurement and user and measurement.customer_id != user.id:
            raise serializers.ValidationError({'measurement_id': 'You can only use your own saved measurements.'})

        if tailor and tailor.role != User.Role.TAILOR:
            raise serializers.ValidationError({'tailor': 'Selected user is not a tailor.'})

        if tailor and hasattr(tailor, 'tailor_profile') and not tailor.tailor_profile.is_active:
            raise serializers.ValidationError({'tailor': 'Selected tailor is not accepting orders right now.'})

        return attrs

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

        if not validated_data.get('delivery_address'):
            validated_data['delivery_address'] = (user.address or '').strip()

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
            # Customer checkout prices fabrics per meter and uses a default 3m estimate.
            subtotal += fabric.price * Decimal('3')
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

    def to_representation(self, instance):
        representation = super().to_representation(instance)
        design_images = get_public_images(representation.get('design_image'), representation.get('design_images'))
        fabric_images = get_public_images(representation.get('fabric_image'), representation.get('fabric_images'))
        representation['design_image'] = get_public_image(representation.get('design_image'), design_images)
        representation['design_images'] = design_images
        representation['fabric_image'] = get_public_image(representation.get('fabric_image'), fabric_images)
        representation['fabric_images'] = fabric_images
        return representation


class TailorOrderDetailSerializer(serializers.ModelSerializer):
    customer_name = serializers.CharField(source='customer.full_name', read_only=True)
    customer_phone = serializers.CharField(read_only=True)
    design_name = serializers.CharField(source='design.title', read_only=True)
    design_image = serializers.CharField(source='design.image', read_only=True)
    design_images = serializers.ListField(source='design.images', child=serializers.CharField(), read_only=True)
    fabric_name = serializers.CharField(source='fabric.material', read_only=True)
    fabric_color = serializers.CharField(source='fabric.color', read_only=True)
    fabric_image = serializers.CharField(source='fabric.image', read_only=True)
    fabric_images = serializers.ListField(source='fabric.images', child=serializers.CharField(), read_only=True)
    measurement = serializers.SerializerMethodField()

    class Meta:
        model = Order
        fields = [
            'id',
            'customer_name',
            'customer_phone',
            'design_name',
            'design_image',
            'design_images',
            'fabric_name',
            'fabric_color',
            'fabric_image',
            'fabric_images',
            'measurement',
            'garment_type',
            'notes',
            'status',
            'total',
            'created_at',
            'updated_at',
        ]

    def get_measurement(self, obj):
        measurement = obj.measurement
        if measurement is None:
            measurement = (
                MeasurementProfile.objects.filter(customer=obj.customer, is_default=True).first()
                or MeasurementProfile.objects.filter(customer=obj.customer).order_by('-created_at').first()
            )
        return MeasurementSerializer(measurement).data if measurement else None

    def to_representation(self, instance):
        representation = super().to_representation(instance)
        design_images = get_public_images(representation.get('design_image'), representation.get('design_images'))
        fabric_images = get_public_images(representation.get('fabric_image'), representation.get('fabric_images'))
        representation['design_image'] = get_public_image(representation.get('design_image'), design_images)
        representation['design_images'] = design_images
        representation['fabric_image'] = get_public_image(representation.get('fabric_image'), fabric_images)
        representation['fabric_images'] = fabric_images
        return representation


class TailorOrderListSerializer(serializers.ModelSerializer):
    customer_name = serializers.CharField(source='customer.full_name', read_only=True)
    customer_phone = serializers.CharField(read_only=True)
    design_name = serializers.CharField(source='design.title', read_only=True)
    design_image = serializers.SerializerMethodField()
    design_images = serializers.SerializerMethodField()
    fabric_name = serializers.CharField(source='fabric.material', read_only=True)
    fabric_color = serializers.CharField(source='fabric.color', read_only=True)
    fabric_image = serializers.SerializerMethodField()
    fabric_images = serializers.SerializerMethodField()

    class Meta:
        model = Order
        fields = [
            'id',
            'customer_name',
            'customer_phone',
            'design_name',
            'design_image',
            'design_images',
            'fabric_name',
            'fabric_color',
            'fabric_image',
            'fabric_images',
            'garment_type',
            'notes',
            'status',
            'total',
            'created_at',
        ]

    def get_design_images(self, obj):
        design = getattr(obj, 'design', None)
        if not design:
            return []
        images = get_public_images(getattr(design, 'image', ''), getattr(design, 'images', []))
        return images[:1]

    def get_design_image(self, obj):
        design = getattr(obj, 'design', None)
        if not design:
            return ''
        return get_public_image(getattr(design, 'image', ''), self.get_design_images(obj))

    def get_fabric_images(self, obj):
        fabric = getattr(obj, 'fabric', None)
        if not fabric:
            return []
        images = get_public_images(getattr(fabric, 'image', ''), getattr(fabric, 'images', []))
        return images[:1]

    def get_fabric_image(self, obj):
        fabric = getattr(obj, 'fabric', None)
        if not fabric:
            return ''
        return get_public_image(getattr(fabric, 'image', ''), self.get_fabric_images(obj))


class PublicTailorSerializer(serializers.ModelSerializer):
    id = serializers.IntegerField(source='user.id', read_only=True)
    name = serializers.CharField(source='user.full_name', read_only=True)
    rating = serializers.DecimalField(max_digits=3, decimal_places=1, read_only=True)
    specialty = serializers.CharField(read_only=True)
    location = serializers.CharField(read_only=True)
    eta = serializers.CharField(read_only=True)
    image = serializers.SerializerMethodField()
    about = serializers.CharField(read_only=True)
    service_price = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True)
    is_featured = serializers.BooleanField(read_only=True)
    is_active = serializers.BooleanField(read_only=True)
    shop_name = serializers.CharField(read_only=True)

    class Meta:
        model = TailorProfile
        fields = [
            'id',
            'name',
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
        ]

    def get_image(self, obj):
        return get_public_image(obj.image, [])


class DashboardTailorSerializer(serializers.ModelSerializer):
    id = serializers.IntegerField(source='user.id', read_only=True)
    name = serializers.CharField(source='user.full_name', read_only=True)
    specialty = serializers.CharField(read_only=True)
    location = serializers.CharField(read_only=True)
    image = serializers.SerializerMethodField()
    rating = serializers.DecimalField(max_digits=3, decimal_places=1, read_only=True)
    service_price = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True)
    is_featured = serializers.BooleanField(read_only=True)
    is_active = serializers.BooleanField(read_only=True)

    class Meta:
        model = TailorProfile
        fields = [
            'id',
            'name',
            'specialty',
            'location',
            'image',
            'rating',
            'service_price',
            'is_featured',
            'is_active',
        ]

    def get_image(self, obj):
        return get_public_image(obj.image, [])


class DashboardFabricSerializer(serializers.ModelSerializer):
    image = serializers.SerializerMethodField()
    images = serializers.SerializerMethodField()

    class Meta:
        model = Fabric
        fields = ['id', 'material', 'color', 'price', 'image', 'images', 'shop', 'description', 'is_active']

    def get_images(self, obj):
        return get_public_images(obj.image, obj.images)

    def get_image(self, obj):
        return get_public_image(obj.image, self.get_images(obj))


class DashboardRecentOrderSerializer(serializers.ModelSerializer):
    tailor_name = serializers.CharField(source='tailor.full_name', read_only=True)
    design_name = serializers.CharField(source='design.title', read_only=True)

    class Meta:
        model = Order
        fields = ['id', 'tailor_name', 'design_name', 'status', 'total', 'created_at']


class DashboardDesignSerializer(serializers.ModelSerializer):
    image = serializers.SerializerMethodField()
    images = serializers.SerializerMethodField()

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
            'base_price',
            'is_active',
            'created_at',
        ]

    def get_images(self, obj):
        return get_public_images(obj.image, obj.images)

    def get_image(self, obj):
        return get_public_image(obj.image, self.get_images(obj))


class DashboardSerializer(serializers.Serializer):
    top_tailors = DashboardTailorSerializer(many=True)
    fabrics = DashboardFabricSerializer(many=True)
    measurements = MeasurementSerializer(many=True)
    recent_orders = DashboardRecentOrderSerializer(many=True)
    designs = DashboardDesignSerializer(many=True)


class TailorShopCatalogSerializer(serializers.Serializer):
    tailor = PublicTailorSerializer()
    fabrics = DashboardFabricSerializer(many=True)
    designs = DashboardDesignSerializer(many=True)


class AdminTailorDetailSerializer(TailorProfileSerializer):
    active_orders = serializers.IntegerField(read_only=True)
    recent_orders = serializers.SerializerMethodField()

    class Meta(TailorProfileSerializer.Meta):
        fields = TailorProfileSerializer.Meta.fields + ['active_orders', 'recent_orders']

    def get_recent_orders(self, obj):
        orders = list(obj.user.tailor_orders.all()[:5])
        return AdminTailorOrderSummarySerializer(orders, many=True).data


class AdminDriverDetailSerializer(DriverProfileSerializer):
    active_deliveries = serializers.IntegerField(read_only=True)
    recent_deliveries = serializers.SerializerMethodField()

    class Meta(DriverProfileSerializer.Meta):
        fields = DriverProfileSerializer.Meta.fields + ['active_deliveries', 'recent_deliveries']

    def get_recent_deliveries(self, obj):
        deliveries = list(obj.user.deliveries.all()[:5])
        return AdminDriverDeliverySummarySerializer(deliveries, many=True).data


class AdminDriverAssignmentSerializer(serializers.ModelSerializer):
    id = serializers.IntegerField(source='user.id', read_only=True)
    name = serializers.CharField(source='user.full_name', read_only=True)
    phone = serializers.CharField(source='user.phone', read_only=True)

    class Meta:
        model = DriverProfile
        fields = ['id', 'name', 'phone', 'vehicle_type', 'is_available']


class AdminOrderListSerializer(serializers.ModelSerializer):
    customer_name = serializers.CharField(source='customer.full_name', read_only=True)
    customer_phone = serializers.CharField(read_only=True)
    tailor_name = serializers.CharField(source='tailor.full_name', read_only=True)
    tailor_phone = serializers.CharField(source='tailor.phone', read_only=True)
    design_name = serializers.CharField(source='design.title', read_only=True)
    design_image = serializers.CharField(source='design.image', read_only=True)
    design_images = serializers.ListField(source='design.images', child=serializers.CharField(), read_only=True)
    fabric_name = serializers.CharField(source='fabric.material', read_only=True)
    fabric_color = serializers.CharField(source='fabric.color', read_only=True)
    fabric_image = serializers.CharField(source='fabric.image', read_only=True)
    fabric_images = serializers.ListField(source='fabric.images', child=serializers.CharField(), read_only=True)
    assigned_driver_name = serializers.SerializerMethodField()
    assigned_driver_id = serializers.SerializerMethodField()

    class Meta:
        model = Order
        fields = [
            'id',
            'customer_name',
            'customer_phone',
            'tailor_name',
            'tailor_phone',
            'design_name',
            'design_image',
            'design_images',
            'fabric_name',
            'fabric_color',
            'fabric_image',
            'fabric_images',
            'status',
            'payment_method',
            'payment_status',
            'subtotal',
            'delivery_fee',
            'total',
            'delivery_address',
            'notes',
            'created_at',
            'assigned_driver_name',
            'assigned_driver_id',
        ]

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

    def to_representation(self, instance):
        representation = super().to_representation(instance)
        design_images = get_public_images(representation.get('design_image'), representation.get('design_images'))
        fabric_images = get_public_images(representation.get('fabric_image'), representation.get('fabric_images'))
        representation['design_image'] = get_public_image(representation.get('design_image'), design_images)
        representation['design_images'] = design_images
        representation['fabric_image'] = get_public_image(representation.get('fabric_image'), fabric_images)
        representation['fabric_images'] = fabric_images
        return representation


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
