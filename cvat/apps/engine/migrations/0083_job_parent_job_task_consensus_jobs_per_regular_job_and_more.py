# Generated by Django 4.2.13 on 2024-08-23 05:25

import cvat.apps.engine.models
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("engine", "0082_alter_labeledimage_job_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="job",
            name="parent_job",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="childern_jobs",
                to="engine.job",
            ),
        ),
        migrations.AddField(
            model_name="task",
            name="consensus_jobs_per_regular_job",
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
