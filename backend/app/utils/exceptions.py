class BotOutboundError(Exception):
    """Base exception for the application."""


class BotHaltedError(BotOutboundError):
    """Raised when the global kill-switch stops a worker path."""


class AccountError(BotOutboundError):
    """Account-related errors."""


class AccountBannedError(AccountError):
    """Account has been banned by Instagram."""


class AccountChallengeError(AccountError):
    """Instagram requires a security challenge (2FA, checkpoint)."""
    def __init__(self, account_id: str, challenge_url: str | None = None):
        self.account_id = account_id
        self.challenge_url = challenge_url
        super().__init__(f"Account {account_id} requires challenge verification")


class AccountCooldownError(AccountError):
    """Account is in cooldown due to rate limiting."""


class NoAvailableAccountError(BotOutboundError):
    """No account available to send messages (all in cooldown/banned/limit reached)."""


class ScraperError(BotOutboundError):
    """Errors during Instagram follower scraping."""


class TargetPrivateError(ScraperError):
    """Target account is private and cannot be scraped."""


class SoftBlockError(ScraperError):
    """Instagram soft-blocked bio fetch requests (community protection response)."""


class ScrapeBudgetError(ScraperError):
    """Raised when no scraping account has remaining daily lookup budget."""


class RateLimitError(BotOutboundError):
    """Instagram rate limit hit."""


class DMSendError(BotOutboundError):
    """Error sending a DM via browser."""


class DMRestrictedError(DMSendError):
    """Target user has DM restrictions (can't receive DMs from non-followers)."""


class DMAbortedBeforeSendError(DMSendError):
    """Raised when the pre-send callback aborts the DM before Enter is pressed.
    The DM was NOT delivered; the message.status was set to 'sending' as a
    pre-flight marker and must be cleaned up by the orchestrator.
    """


class OllamaError(BotOutboundError):
    """Error communicating with Ollama."""


class CampaignError(BotOutboundError):
    """Campaign lifecycle errors."""
