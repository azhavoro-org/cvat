# Copyright (C) 2024 CVAT.ai Corporation
#
# SPDX-License-Identifier: MIT

from typing import Dict, List

import django_rq
from datumaro.components.operations import IntersectMerge
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response

import datumaro as dm
from cvat.apps.dataset_manager.bindings import import_dm_annotations
from cvat.apps.dataset_manager.task import PatchAction, patch_job_data
from cvat.apps.engine.models import Job, JobType, Task
from cvat.apps.engine.serializers import RqIdSerializer
from cvat.apps.engine.utils import (define_dependent_job, get_rq_job_meta,
                                    get_rq_lock_by_user, process_failed_job)
from cvat.apps.quality_control.quality_reports import JobDataProvider


def get_consensus_jobs(task_id: int) -> Dict[int, List[int]]:
    jobs = {}  # parent_job_id -> [consensus_job_id]

    for job in Job.objects.select_related("segment").filter(
        segment__task_id=task_id, type=JobType.CONSENSUS.value
    ):
        assert job.parent_job_id
        jobs.setdefault(job.parent_job_id, []).append(job.id)

    return jobs


def get_annotations(job_id: int) -> dm.Dataset:
    return JobDataProvider(job_id).dm_dataset


@transaction.atomic
def _merge_consensus_jobs(task_id: int) -> None:
    jobs = get_consensus_jobs(task_id)
    consensus_settings = ConsensusSettings.objects.filter(task=task_id).first()
    merger = IntersectMerge(
        conf=IntersectMerge.Conf(
            pairwise_dist=consensus_settings.iou_threshold,
            output_conf_thresh=consensus_settings.agreement_score_threshold,
            quorum=consensus_settings.quorum,
        )
    )

    for parent_job_id, job_ids in jobs.items():
        consensus_dataset = list(map(get_annotations, job_ids))

        merged_dataset = merger(consensus_dataset)

        # delete the existing annotations in the job
        patch_job_data(parent_job_id, None, PatchAction.DELETE)
        # if we don't delete exising annotations, the imported annotations
        # will be appended to the existing annotations, and thus updated annotation
        # would have both exisiting + imported annotations, but we only want the
        # imported annotations

        parent_job = JobDataProvider(parent_job_id)

        # imports the annotations in the `parent_job.job_data` instance
        import_dm_annotations(merged_dataset, parent_job.job_data)

        # updates the annotations in the job
        patch_job_data(
            parent_job_id, parent_job.job_data.data.serialize(), PatchAction.UPDATE
        )


def merge_task(task: Task, request) -> Response:
    queue_name = settings.CVAT_QUEUES.CONSENSUS.value
    queue = django_rq.get_queue(queue_name)
    # so a user doesn't create requests to merge same task multiple times
    rq_id = request.data.get(
        "rq_id", f"merge_consensus:task.id{task.id}-by-{request.user}"
    )
    rq_job = queue.fetch_job(rq_id)
    user_id = request.user.id

    if rq_job:
        if rq_job.is_finished:
            # returned_data = rq_job.return_value()
            rq_job.delete()
            return Response(
                status=status.HTTP_201_CREATED
            )  # if returned_data == 201 else Response(status=status.HTTP_400_BAD_REQUEST)
        elif rq_job.is_failed:
            exc_info = process_failed_job(rq_job)
            return Response(data=exc_info, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        else:
            # rq_job is in queued stage or might be running
            return Response(status=status.HTTP_202_ACCEPTED)
            # return Response(serializer.data, status=status.HTTP_202_ACCEPTED)

    func = _merge_consensus_jobs
    func_args = [task.id]

    with get_rq_lock_by_user(queue, user_id):
        queue.enqueue_call(
            func=func,
            args=func_args,
            job_id=rq_id,
            meta=get_rq_job_meta(request=request, db_obj=task),
            depends_on=define_dependent_job(queue, user_id),
        )

    return Response(status=status.HTTP_202_ACCEPTED)
    # serializer = RqIdSerializer(data={'rq_id': rq_id})
    # serializer.is_valid(raise_exception=True)
    # return Response(serializer.data, status=status.HTTP_202_ACCEPTED)
