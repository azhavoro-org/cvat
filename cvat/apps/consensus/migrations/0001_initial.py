# Generated by Django 4.2.11 on 2024-06-27 11:47

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ("engine", "0079_job_parent_job_id_task_consensus_jobs_per_segment"),
    ]

    operations = [
        migrations.CreateModel(
            name="ConsensusSettings",
            fields=[
                (
                    "id",
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("agreement_score_threshold", models.FloatField()),
                ("quorum", models.IntegerField()),
                ("iou_threshold", models.FloatField()),
                (
                    "task",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="consensus_settings",
                        to="engine.task",
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="ConsensusReport",
            fields=[
                (
                    "id",
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("created_date", models.DateTimeField(auto_now_add=True)),
                ("data", models.JSONField()),
                (
                    "task",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="consensus_reports",
                        to="engine.task",
                    ),
                ),
            ],
        ),
    ]
