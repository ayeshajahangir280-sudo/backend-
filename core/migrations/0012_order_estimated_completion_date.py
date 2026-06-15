from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0011_user_image'),
    ]

    operations = [
        migrations.AddField(
            model_name='order',
            name='estimated_completion_date',
            field=models.DateField(blank=True, null=True),
        ),
    ]
