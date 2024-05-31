# Generated by Django 4.2.11 on 2024-05-31 02:07

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("engine", "0083_alter_task_consensus_job_per_segment"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="task",
            name="consensus_job_per_segment",
        ),
        migrations.AddField(
            model_name="data",
            name="consensus_job_per_segment",
            field=models.PositiveIntegerField(blank=True, default=1),
        ),
    ]
