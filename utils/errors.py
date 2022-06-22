from discord.ext import commands


IGNORED = (
    commands.CommandNotFound,
    commands.NotOwner,
    commands.CheckFailure,
)
SEND = (
    TypeError,
    commands.GuildNotFound,
    commands.UserNotFound,
    commands.BadArgument,
    commands.MissingRequiredArgument,
    commands.TooManyArguments,
    commands.UserInputError,
)
