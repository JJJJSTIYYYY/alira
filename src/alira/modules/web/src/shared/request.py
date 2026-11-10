"""Web request and payload contracts available to dependent modules."""

from dataclasses import dataclass, field

from alira.modules.agent.src.shared.identity import AliraIdentity


@dataclass
class BasePayload:
    _identity: AliraIdentity | None = field(default=None, init=False)
    _request_id: str | None = field(default=None, init=False)

    @property
    def identity(self) -> AliraIdentity | None:
        return self._identity

    @property
    def request_id(self) -> str | None:
        return self._request_id


@dataclass
class BaseRequest:
    request_id: str
    identity: AliraIdentity
    payload: BasePayload | None = field(default=None, kw_only=True)

    def __post_init__(self):
        if not self.request_id:
            raise ValueError("request_id must be provided")

        if self.identity is None:
            raise ValueError("identity must be provided")

        if self.payload is not None:
            self.payload._request_id = self.request_id
            self.payload._identity = self.identity
