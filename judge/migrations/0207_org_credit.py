# Generated by Django 3.2.19 on 2024-09-19 02:02

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('judge', '0206_monthly_credit'),
    ]

    operations = [
        migrations.AddField(
            model_name='organization',
            name='available_credit',
            field=models.FloatField(default=0, help_text='Available credits'),
        ),
        migrations.AddField(
            model_name='organization',
            name='current_consumed_credit',
            field=models.FloatField(default=0, help_text='Total used credit this month'),
        ),
        migrations.AddField(
            model_name='organization',
            name='monthly_credit',
            field=models.FloatField(default=0, help_text='Total monthly free credit left'),
        ),
    ]
