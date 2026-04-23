from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0007_fabric_images'),
    ]

    operations = [
        migrations.AlterField(
            model_name='user',
            name='role',
            field=models.CharField(
                choices=[('customer', 'Customer'), ('tailor', 'Tailor'), ('driver', 'Driver'), ('admin', 'Admin')],
                db_index=True,
                default='customer',
                max_length=20,
            ),
        ),
        migrations.AddIndex(
            model_name='tailorprofile',
            index=models.Index(fields=['is_active', 'is_featured'], name='core_tailor_is_acti_113877_idx'),
        ),
        migrations.AddIndex(
            model_name='driverprofile',
            index=models.Index(fields=['is_available'], name='core_driver_is_avai_4409ff_idx'),
        ),
        migrations.AddIndex(
            model_name='measurementprofile',
            index=models.Index(fields=['customer', 'is_default', '-created_at'], name='core_measur_customer_6c5489_idx'),
        ),
        migrations.AddIndex(
            model_name='fabric',
            index=models.Index(fields=['is_active', '-created_at'], name='core_fabric_is_acti_949bb6_idx'),
        ),
        migrations.AddIndex(
            model_name='fabric',
            index=models.Index(fields=['uploaded_by', 'is_active', '-created_at'], name='core_fabric_uploade_8a4ff1_idx'),
        ),
        migrations.AddIndex(
            model_name='design',
            index=models.Index(fields=['is_active', '-created_at'], name='core_design_is_acti_4bc757_idx'),
        ),
        migrations.AddIndex(
            model_name='design',
            index=models.Index(fields=['uploaded_by', 'is_active', '-created_at'], name='core_design_uploade_1fe40d_idx'),
        ),
        migrations.AddIndex(
            model_name='order',
            index=models.Index(fields=['customer', '-created_at'], name='core_order_customer_444fd0_idx'),
        ),
        migrations.AddIndex(
            model_name='order',
            index=models.Index(fields=['tailor', '-created_at'], name='core_order_tailor_9be1c2_idx'),
        ),
        migrations.AddIndex(
            model_name='order',
            index=models.Index(fields=['tailor', 'status', '-created_at'], name='core_order_tailor_827772_idx'),
        ),
        migrations.AddIndex(
            model_name='order',
            index=models.Index(fields=['customer', 'status', '-created_at'], name='core_order_customer_8118c0_idx'),
        ),
        migrations.AddIndex(
            model_name='delivery',
            index=models.Index(fields=['driver', 'status', '-assigned_date'], name='core_delive_driver__5d95cf_idx'),
        ),
    ]
