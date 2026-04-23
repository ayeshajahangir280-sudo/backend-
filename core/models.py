from django.contrib.auth.base_user import BaseUserManager
from django.contrib.auth.models import AbstractUser
from django.db import models


class UserManager(BaseUserManager):
    def create_user(self, email, password=None, **extra_fields):
        if not email:
            raise ValueError('Email is required.')
        email = self.normalize_email(email)
        username = extra_fields.pop('username', '') or email
        user = self.model(email=email, username=username, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email, password=None, **extra_fields):
        extra_fields.setdefault('is_staff', True)
        extra_fields.setdefault('is_superuser', True)
        extra_fields.setdefault('role', User.Role.ADMIN)

        if extra_fields.get('is_staff') is not True:
            raise ValueError('Superuser must have is_staff=True.')
        if extra_fields.get('is_superuser') is not True:
            raise ValueError('Superuser must have is_superuser=True.')

        return self.create_user(email, password, **extra_fields)


class User(AbstractUser):
    class Role(models.TextChoices):
        CUSTOMER = 'customer', 'Customer'
        TAILOR = 'tailor', 'Tailor'
        DRIVER = 'driver', 'Driver'
        ADMIN = 'admin', 'Admin'

    username = models.CharField(max_length=150, unique=True, blank=True)
    email = models.EmailField(unique=True)
    full_name = models.CharField(max_length=255)
    phone = models.CharField(max_length=30, blank=True)
    role = models.CharField(max_length=20, choices=Role.choices, default=Role.CUSTOMER, db_index=True)
    address = models.TextField(blank=True)

    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS = ['full_name']

    objects = UserManager()

    def save(self, *args, **kwargs):
        if not self.username:
            self.username = self.email
        super().save(*args, **kwargs)

    def __str__(self):
        return f'{self.full_name} ({self.role})'


class TailorProfile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='tailor_profile')
    specialty = models.CharField(max_length=255, blank=True)
    location = models.CharField(max_length=255, blank=True)
    eta = models.CharField(max_length=100, blank=True)
    image = models.TextField(blank=True)
    about = models.TextField(blank=True)
    service_price = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    rating = models.DecimalField(max_digits=3, decimal_places=1, default=0)
    is_featured = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    national_id = models.CharField(max_length=50, blank=True)
    shop_name = models.CharField(max_length=255, blank=True)
    bank_name = models.CharField(max_length=255, blank=True)
    account_title = models.CharField(max_length=255, blank=True)
    account_number = models.CharField(max_length=100, blank=True)
    iban = models.CharField(max_length=100, blank=True)

    def __str__(self):
        return self.user.full_name

    class Meta:
        indexes = [
            models.Index(fields=['is_active', 'is_featured']),
        ]


class DriverProfile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='driver_profile')
    vehicle_type = models.CharField(max_length=120, blank=True)
    vehicle_number = models.CharField(max_length=50, blank=True)
    license_number = models.CharField(max_length=100, blank=True)
    is_available = models.BooleanField(default=True)
    national_id = models.CharField(max_length=50, blank=True)
    bank_name = models.CharField(max_length=255, blank=True)
    account_title = models.CharField(max_length=255, blank=True)
    account_number = models.CharField(max_length=100, blank=True)
    iban = models.CharField(max_length=100, blank=True)

    def __str__(self):
        return self.user.full_name

    class Meta:
        indexes = [
            models.Index(fields=['is_available']),
        ]


class MeasurementProfile(models.Model):
    customer = models.ForeignKey(User, on_delete=models.CASCADE, related_name='measurements')
    name = models.CharField(max_length=120)
    chest = models.DecimalField(max_digits=6, decimal_places=2)
    waist = models.DecimalField(max_digits=6, decimal_places=2)
    shoulder = models.DecimalField(max_digits=6, decimal_places=2)
    sleeve = models.DecimalField(max_digits=6, decimal_places=2)
    height = models.DecimalField(max_digits=6, decimal_places=2)
    length = models.DecimalField(max_digits=6, decimal_places=2)
    is_default = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-is_default', '-created_at']
        indexes = [
            models.Index(fields=['customer', 'is_default', '-created_at']),
        ]

    def __str__(self):
        return f'{self.customer.full_name} - {self.name}'


class Fabric(models.Model):
    material = models.CharField(max_length=120)
    color = models.CharField(max_length=120)
    price = models.DecimalField(max_digits=10, decimal_places=2)
    image = models.TextField(blank=True)
    images = models.JSONField(default=list, blank=True)
    shop = models.CharField(max_length=150, blank=True)
    description = models.TextField(blank=True)
    uploaded_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='uploaded_fabrics')
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['material']
        indexes = [
            models.Index(fields=['is_active', '-created_at']),
            models.Index(fields=['uploaded_by', 'is_active', '-created_at']),
        ]

    def __str__(self):
        return f'{self.material} - {self.color}'


class Design(models.Model):
    title = models.CharField(max_length=150)
    category = models.CharField(max_length=120)
    image = models.TextField(blank=True)
    images = models.JSONField(default=list, blank=True)
    description = models.TextField()
    compatible_fabrics = models.JSONField(default=list, blank=True)
    designer = models.CharField(max_length=150, blank=True)
    uploaded_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='uploaded_designs')
    base_price = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['title']
        indexes = [
            models.Index(fields=['is_active', '-created_at']),
            models.Index(fields=['uploaded_by', 'is_active', '-created_at']),
        ]

    def __str__(self):
        return self.title


class Order(models.Model):
    class Status(models.TextChoices):
        PLACED = 'Placed', 'Placed'
        RECEIVED = 'Received', 'Received'
        CONFIRMED = 'Confirmed', 'Confirmed'
        ACCEPTED = 'Accepted', 'Accepted'
        REJECTED = 'Rejected', 'Rejected'
        IN_STITCHING = 'In Stitching', 'In Stitching'
        READY = 'Ready', 'Ready'
        OUT_FOR_DELIVERY = 'Out for Delivery', 'Out for Delivery'
        DELIVERED = 'Delivered', 'Delivered'
        CANCELLED = 'Cancelled', 'Cancelled'

    class PaymentMethod(models.TextChoices):
        CARD = 'card', 'Card'
        CASH = 'cash', 'Cash'
        BANK_TRANSFER = 'bank_transfer', 'Bank Transfer'
        WALLET = 'wallet', 'Wallet'

    class PaymentStatus(models.TextChoices):
        PENDING = 'pending', 'Pending'
        PAID = 'paid', 'Paid'
        FAILED = 'failed', 'Failed'
        REFUNDED = 'refunded', 'Refunded'

    customer = models.ForeignKey(User, on_delete=models.CASCADE, related_name='customer_orders')
    tailor = models.ForeignKey(User, on_delete=models.CASCADE, related_name='tailor_orders')
    design = models.ForeignKey(Design, on_delete=models.SET_NULL, null=True, blank=True)
    fabric = models.ForeignKey(Fabric, on_delete=models.SET_NULL, null=True, blank=True)
    measurement = models.ForeignKey(MeasurementProfile, on_delete=models.SET_NULL, null=True, blank=True)
    garment_type = models.CharField(max_length=120, blank=True)
    notes = models.TextField(blank=True)
    status = models.CharField(max_length=30, choices=Status.choices, default=Status.RECEIVED)
    payment_method = models.CharField(max_length=30, choices=PaymentMethod.choices, default=PaymentMethod.CARD)
    payment_status = models.CharField(max_length=30, choices=PaymentStatus.choices, default=PaymentStatus.PENDING)
    subtotal = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    delivery_fee = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    total = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    customer_phone = models.CharField(max_length=30, blank=True)
    delivery_address = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['customer', '-created_at']),
            models.Index(fields=['tailor', '-created_at']),
            models.Index(fields=['tailor', 'status', '-created_at']),
            models.Index(fields=['customer', 'status', '-created_at']),
        ]

    def __str__(self):
        return f'Order #{self.pk}'


class Delivery(models.Model):
    class Status(models.TextChoices):
        PENDING_ASSIGNMENT = 'Pending Assignment', 'Pending Assignment'
        ASSIGNED = 'Assigned', 'Assigned'
        PICKED_UP = 'Picked Up', 'Picked Up'
        IN_TRANSIT = 'In Transit', 'In Transit'
        DELIVERED = 'Delivered', 'Delivered'

    order = models.OneToOneField(Order, on_delete=models.CASCADE, related_name='delivery')
    driver = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='deliveries')
    pickup_address = models.TextField(blank=True)
    delivery_address = models.TextField(blank=True)
    status = models.CharField(max_length=30, choices=Status.choices, default=Status.PENDING_ASSIGNMENT)
    assigned_date = models.DateField(null=True, blank=True)
    pickup_time = models.DateTimeField(null=True, blank=True)
    delivery_time = models.DateTimeField(null=True, blank=True)
    admin_notes = models.TextField(blank=True)

    class Meta:
        ordering = ['-id']
        indexes = [
            models.Index(fields=['driver', 'status', '-assigned_date']),
        ]

    def __str__(self):
        return f'Delivery #{self.pk}'
