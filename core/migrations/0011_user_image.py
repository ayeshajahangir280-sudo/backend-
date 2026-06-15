from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0010_backfill_public_tailor_designs_and_hide_debug_records'),
    ]

    operations = [
        migrations.AddField(
            model_name='user',
            name='image',
            field=models.TextField(blank=True),
        ),
    ]
