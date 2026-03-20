from .bb_trsupport import BBTRSupport


async def setup(bot):
    await bot.add_cog(BBTRSupport(bot))
