from __future__ import annotations

import logging
from dataclasses import asdict
from datetime import timezone
from typing import Any, Mapping, Optional, Sequence, Tuple, TypedDict
from urllib.parse import quote

from isodate import parse_datetime

from sentry.integrations.gitlab.utils import (
    GitLabApiClientPath,
    GitLabRateLimitInfo,
    get_rate_limit_info_from_response,
)
from sentry.integrations.mixins.commit_context import CommitInfo, FileBlameInfo, SourceLineInfo
from sentry.shared_integrations.client.base import BaseApiClient
from sentry.shared_integrations.exceptions import ApiRateLimitedError
from sentry.shared_integrations.exceptions.base import ApiError
from sentry.shared_integrations.response.sequence import SequenceApiResponse
from sentry.utils import json, metrics

logger = logging.getLogger("sentry.integrations.gitlab")


MINIMUM_REQUESTS = 100


class GitLabCommitResponse(TypedDict):
    id: str
    message: Optional[str]
    committed_date: Optional[str]
    author_name: Optional[str]
    author_email: Optional[str]
    committer_name: Optional[str]
    committer_email: Optional[str]


class GitLabFileBlameResponseItem(TypedDict):
    commit: GitLabCommitResponse
    lines: Sequence[str]


def fetch_file_blames(
    client: BaseApiClient, files: Sequence[SourceLineInfo], extra: Mapping[str, Any]
) -> list[FileBlameInfo]:
    blames = []

    for i, file in enumerate(files):
        try:
            commit, rate_limit_info = _fetch_file_blame(client, file, extra)
            if commit:
                blames.append(_create_file_blame_info(commit, file))
        except ApiError as e:
            _handle_file_blame_error(e, file, extra)
        else:
            # On first iteration, make sure we have enough requests left
            if (
                i == 0
                and len(files) > 1
                and rate_limit_info
                and rate_limit_info.remaining < (MINIMUM_REQUESTS - len(files))
            ):
                metrics.incr("integrations.gitlab.get_blame_for_files.rate_limit")
                logger.exception(
                    "get_blame_for_files.rate_limit_too_low",
                    extra={
                        **extra,
                        "num_files": len(files),
                        "remaining_requests": rate_limit_info.remaining,
                        "total_requests": rate_limit_info.limit,
                        "next_window": rate_limit_info.next_window(),
                    },
                )
                raise ApiRateLimitedError("Approaching GitLab API rate limit")

    return blames


def _fetch_file_blame(
    client: BaseApiClient, file: SourceLineInfo, extra: Mapping[str, Any]
) -> Tuple[Optional[CommitInfo], Optional[GitLabRateLimitInfo]]:
    project_id = file.repo.config.get("project_id")
    encoded_path = quote(file.path, safe="")
    request_path = GitLabApiClientPath.blame.format(project=project_id, path=encoded_path)
    params = {"ref": file.ref, "range[start]": file.lineno, "range[end]": file.lineno}
    cache_key = client.get_cache_key(request_path, json.dumps(params))
    response = client.check_cache(cache_key)
    if response:
        logger.info(
            "sentry.integrations.gitlab.get_blame_for_files.got_cached",
            extra=extra,
        )
    else:
        response = client.get(
            request_path,
            params=params,
        )
        client.set_cache(cache_key, response, 60)

    if not isinstance(response, SequenceApiResponse):
        raise ApiError("Response is not in expected format")

    rate_limit_info = get_rate_limit_info_from_response(response)

    return _get_commit_info_from_blame_response(response, extra=extra), rate_limit_info


def _create_file_blame_info(commit: CommitInfo, file: SourceLineInfo) -> FileBlameInfo:
    return FileBlameInfo(
        **asdict(file),
        commit=commit,
    )


def _handle_file_blame_error(error: ApiError, file: SourceLineInfo, extra: Mapping[str, Any]):
    if error.code == 429:
        metrics.incr("sentry.integrations.gitlab.get_blame_for_files.rate_limit")
    logger.exception(
        "get_blame_for_files.api_error",
        extra={
            **extra,
            "repo_name": file.repo.name,
            "file_path": file.path,
            "branch_name": file.ref,
            "file_lineno": file.lineno,
        },
    )


def _get_commit_info_from_blame_response(
    response: Optional[Sequence[GitLabFileBlameResponseItem]], extra: Mapping[str, Any]
) -> Optional[CommitInfo]:
    if response is None:
        return None

    commits = [_create_commit_from_blame(item.get("commit"), extra) for item in response]
    commits_with_required_info = [commit for commit in commits if commit is not None]

    if not commits_with_required_info:
        return None

    return max(commits_with_required_info, key=lambda commit: commit.committedDate)


def _create_commit_from_blame(
    commit: Optional[GitLabCommitResponse], extra: Mapping[str, Any]
) -> Optional[CommitInfo]:
    if not commit:
        logger.warning("get_blame_for_files.no_commit_in_response", extra=extra)
        return None

    commit_id = commit.get("id")
    committed_date = commit.get("committed_date")

    if not commit_id:
        logger.warning(
            "get_blame_for_files.invalid_commit_response", extra={**extra, "missing_property": "id"}
        )
        return None

    if not committed_date:
        logger.warning(
            "get_blame_for_files.invalid_commit_response",
            extra={**extra, "commit_id": commit_id, "missing_property": "committed_date"},
        )
        return None

    try:
        return CommitInfo(
            commitId=commit_id,
            commitMessage=commit.get("message"),
            commitAuthorName=commit.get("author_name"),
            commitAuthorEmail=commit.get("author_email"),
            committedDate=parse_datetime(committed_date).replace(tzinfo=timezone.utc),
        )
    except Exception:
        logger.exception("get_blame_for_files.invalid_commit_response", extra=extra)
        return None
