from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0006_alter_order_status'),
    ]

    operations = [
        migrations.AddField(
            model_name='fabric',
            name='images',
            field=models.JSONField(blank=True, default=list),
        ),
    ]
