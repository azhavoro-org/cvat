# Generated by Django 4.2.13 on 2024-07-07 09:18

import cvat.apps.engine.models
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("engine", "0078_alter_cloudstorage_credentials"),
    ]

    operations = [
        migrations.AddField(
            model_name="job",
            name="parent_job_id",
            field=models.PositiveIntegerField(blank=True, default=None, null=True),
        ),
        migrations.AddField(
            model_name="task",
            name="consensus_jobs_per_normal_job",
            field=models.IntegerField(blank=True, default=0),
        ),
        migrations.AlterField(
            model_name="job",
            name="type",
            field=models.CharField(
                choices=[
                    ("annotation", "ANNOTATION"),
                    ("ground_truth", "GROUND_TRUTH"),
                    ("consensus", "CONSENSUS"),
                ],
                default=cvat.apps.engine.models.JobType["ANNOTATION"],
                max_length=32,
            ),
        ),
    ]
