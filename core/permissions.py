from rest_framework.permissions import BasePermission

from .models import User


class IsRole(BasePermission):
    allowed_roles = set()

    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated and request.user.role in self.allowed_roles)


class IsCustomer(IsRole):
    allowed_roles = {User.Role.CUSTOMER, User.Role.ADMIN}


class IsTailor(IsRole):
    allowed_roles = {User.Role.TAILOR, User.Role.ADMIN}


class IsDriver(IsRole):
    allowed_roles = {User.Role.DRIVER, User.Role.ADMIN}
