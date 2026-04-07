from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin

from .models import Delivery, Design, DriverProfile, Fabric, MeasurementProfile, Order, TailorProfile, User


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    list_display = ('email', 'full_name', 'role', 'phone', 'is_staff')
    ordering = ('email',)
    fieldsets = (
        (None, {'fields': ('email', 'password')}),
        ('Personal info', {'fields': ('full_name', 'phone', 'role', 'address')}),
        ('Permissions', {'fields': ('is_active', 'is_staff', 'is_superuser', 'groups', 'user_permissions')}),
        ('Important dates', {'fields': ('last_login', 'date_joined')}),
    )
    add_fieldsets = (
        (
            None,
            {
                'classes': ('wide',),
                'fields': ('email', 'full_name', 'phone', 'role', 'password1', 'password2'),
            },
        ),
    )


@admin.register(TailorProfile)
class TailorProfileAdmin(admin.ModelAdmin):
    list_display = ('user', 'shop_name', 'specialty', 'location', 'phone_number', 'rating', 'is_featured', 'is_active')
    list_filter = ('is_featured', 'is_active', 'location')
    search_fields = ('user__full_name', 'specialty', 'location')

    def phone_number(self, obj):
        return obj.user.phone


@admin.register(DriverProfile)
class DriverProfileAdmin(admin.ModelAdmin):
    list_display = ('user', 'vehicle_type', 'vehicle_number', 'phone_number', 'is_available')
    list_filter = ('is_available',)

    def phone_number(self, obj):
        return obj.user.phone


@admin.register(MeasurementProfile)
class MeasurementProfileAdmin(admin.ModelAdmin):
    list_display = ('customer', 'name', 'is_default', 'created_at')
    list_filter = ('is_default',)
    search_fields = ('customer__full_name', 'name')


@admin.register(Fabric)
class FabricAdmin(admin.ModelAdmin):
    list_display = ('material', 'color', 'price', 'shop', 'is_active')
    list_filter = ('is_active', 'shop')


@admin.register(Design)
class DesignAdmin(admin.ModelAdmin):
    list_display = ('title', 'category', 'designer', 'base_price', 'is_active')
    list_filter = ('category', 'is_active')


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = ('id', 'customer', 'tailor', 'payment_method', 'payment_status', 'status', 'total', 'created_at')
    list_filter = ('status', 'payment_method', 'payment_status')
    search_fields = ('customer__full_name', 'tailor__full_name')


@admin.register(Delivery)
class DeliveryAdmin(admin.ModelAdmin):
    list_display = ('id', 'order', 'driver', 'status', 'assigned_date')
    list_filter = ('status',)
