import discord
from discord.ext import commands


class BlankException(Exception):
    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)

    def __str__(self) -> str:
        return self.message


# https://github.com/Tom-the-Bomb/BombBot/blob/master/bot/utils/imaging/exceptions.py#L34-L42
class ImageTooLarge(BlankException):
    def __init__(self, size: int, max_size: int = 15_000_000) -> None:
        MIL = 1_000_000
        self.message = (
            f"The size of the provided image (`{size / MIL:.2f} MB`) "
            f"exceeds the limit of `{max_size / MIL} MB`"
        )
        super().__init__(self.message)


class DoNothing(Exception):
    pass


class NoImageFound(BlankException):
    pass


class DevError(BlankException):
    pass


class BadTimeTransform(BlankException):
    pass


class UnknownAccount(BlankException):
    pass


class NoTwemojiFound(BlankException):
    pass


class VideoIsLive(BlankException):
    pass


class NotTenorUrl(BlankException):
    pass


class ResponseError(Exception):
    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)

    def __str__(self) -> str:
        return self.message


class ServerErrorResponse(ResponseError):
    pass


class BadGateway(ServerErrorResponse):
    pass


class ClientErrorResponse(ResponseError):
    pass


class NotFound(ClientErrorResponse):
    pass


class Unauthorized(ClientErrorResponse):
    pass


class BadRequest(ClientErrorResponse):
    pass


class Forbidden(ClientErrorResponse):
    pass


class InvalidColor(commands.BadArgument):
    pass


class RateLimitExceeded(commands.BadArgument):
    pass


class NoCover(commands.BadArgument):
    pass


class InvalidDateProvided(commands.BadArgument):
    pass


IGNORED = (
    commands.CommandNotFound,
    commands.NotOwner,
    commands.CheckFailure,
    DoNothing,
)
SEND = (
    TypeError,
    ValueError,
    discord.HTTPException,
    commands.GuildNotFound,
    commands.UserNotFound,
    commands.BadArgument,
    commands.MissingRequiredArgument,
    commands.TooManyArguments,
    commands.UserInputError,
    commands.BotMissingPermissions,
    commands.MissingPermissions,
    ResponseError,
    NoImageFound,
    BlankException,
    VideoIsLive,
    NotTenorUrl,
    UnknownAccount,
    ServerErrorResponse,
    BadGateway,
    ClientErrorResponse,
    NotFound,
    Unauthorized,
    BadRequest,
    Forbidden,
    ImageTooLarge,
    InvalidColor,
    RateLimitExceeded,
    NoCover,
)
