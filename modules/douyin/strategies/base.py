from abc import ABC, abstractmethod

from ..model import DouyinParseResult


class StrategyParams:
    def __init__(self, url: str, cookie: str = "", api_url: str = ""):
        self.url = url
        self.cookie = cookie
        self.api_url = api_url


class BaseStrategy(ABC):
    @property
    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    async def execute(self, params: StrategyParams) -> DouyinParseResult: ...
