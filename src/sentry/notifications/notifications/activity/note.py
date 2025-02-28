from __future__ import annotations

from typing import Any, Mapping, Optional

from sentry.services.hybrid_cloud.actor import RpcActor
from sentry.types.integrations import ExternalProviders

from .base import GroupActivityNotification


class NoteActivityNotification(GroupActivityNotification):
    message_builder = "SlackNotificationsMessageBuilder"
    metrics_key = "note_activity"
    template_path = "sentry/emails/activity/note"

    def get_description(self) -> tuple[str, Optional[str], Mapping[str, Any]]:
        # Notes may contain {} characters so we should escape them.
        text = str(self.activity.data["text"]).replace("{", "{{").replace("}", "}}")
        return text, None, {}

    @property
    def title(self) -> str:
        if self.user:
            author = self.user.get_display_name()
        else:
            author = "Unknown"
        return f"New comment by {author}"

    def get_notification_title(
        self, provider: ExternalProviders, context: Mapping[str, Any] | None = None
    ) -> str:
        return self.title

    def get_message_description(self, recipient: RpcActor, provider: ExternalProviders) -> Any:
        return self.get_context()["text_description"]
