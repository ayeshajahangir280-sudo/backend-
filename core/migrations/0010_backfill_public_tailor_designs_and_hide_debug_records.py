from django.db import migrations


DEBUG_TAILOR_SHOP_NAMES = ('Debug Shop', 'Debug Shop Json')
DEBUG_DESIGN_TITLES = ('Debug Multipart Design',)


def backfill_public_tailor_designs_and_hide_debug_records(apps, schema_editor):
    Design = apps.get_model('core', 'Design')
    Fabric = apps.get_model('core', 'Fabric')
    TailorProfile = apps.get_model('core', 'TailorProfile')

    Design.objects.filter(uploaded_by__role='tailor').update(is_active=True)

    TailorProfile.objects.filter(shop_name__in=DEBUG_TAILOR_SHOP_NAMES).update(
        is_active=False,
        is_featured=False,
    )

    Design.objects.filter(uploaded_by__tailor_profile__shop_name__in=DEBUG_TAILOR_SHOP_NAMES).update(is_active=False)
    Design.objects.filter(designer__in=DEBUG_TAILOR_SHOP_NAMES).update(is_active=False)
    Design.objects.filter(title__in=DEBUG_DESIGN_TITLES).update(is_active=False)

    Fabric.objects.filter(uploaded_by__tailor_profile__shop_name__in=DEBUG_TAILOR_SHOP_NAMES).update(is_active=False)
    Fabric.objects.filter(shop__in=DEBUG_TAILOR_SHOP_NAMES).update(is_active=False)


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0009_usersession_and_more'),
    ]

    operations = [
        migrations.RunPython(backfill_public_tailor_designs_and_hide_debug_records, noop_reverse),
    ]
