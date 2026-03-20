"""
BB TR Support — Red-bot cog
Bridges Discord to the Trading Ranch WordPress support ticket system.

Users type in the support channel; their message is deleted and a **private
thread** is created automatically.  Staff with the configured role (or
Manage Threads permission) can see every private thread.

Commands:
  !support                         — points users to the support channel
  !trsupport setchannel #channel   — set support channel              (admin)
  !trsupport setsecret <key>       — set WordPress API secret         (admin)
  !trsupport seturl <url>          — set WordPress site URL           (admin)
  !trsupport setstaffrole @role    — set support staff role           (admin)
  !trsupport instructions          — post welcome embed in channel    (admin)
  !trsupport status <id> <status>  — update a ticket's status        (staff)
  !trsupport view <id>             — view a ticket summary            (staff)
  !trsupport close <id>            — close a ticket                   (staff)
  !trsupport claim <id>            — claim / assign a ticket to you   (staff)
  !trsupport reply <id> <msg>      — reply to a ticket directly       (staff)
  !trsupport list [status]         — list tickets                     (staff)
  !trsupport ping                  — test connection to WordPress     (staff)
  !trsupport settings              — show current configuration       (admin)

Thread relay:
  Any message posted in a ticket thread is forwarded to WordPress as a reply.
  Staff can prefix a message with [note] to mark it as an internal note.
  Web replies are automatically synced back to the Discord thread.
"""

import asyncio
import logging
import discord
import aiohttp

from redbot.core import commands, Config
from redbot.core.bot import Red

log = logging.getLogger("red.bb_trsupport")

# ── Topic list — must match bb_trs_topics() in BB-TR-Support.php ─────────────
TOPICS = [
    ("general",     "General"),
    ("ninjatrader", "NinjaTrader Help"),
    ("tradingview", "TradingView Help"),
    ("discord",     "Discord Help"),
    ("other",       "Other"),
]

STATUSES = ["open", "pending", "resolved", "closed"]

STATUS_COLORS = {
    "open":     0x2563EB,
    "pending":  0xD97706,
    "resolved": 0x16A34A,
    "closed":   0x6B7280,
}

SYNC_INTERVAL = 60  # seconds between WP → Discord reply sync checks


class BBTRSupport(commands.Cog):
    """Trading Ranch WordPress support ticket bridge."""

    def __init__(self, bot: Red):
        self.bot = bot
        self.session: aiohttp.ClientSession = None
        self._sync_task: asyncio.Task = None
        self.config = Config.get_conf(self, identifier=8675309421, force_registration=True)
        self.config.register_global(
            wp_url          = "https://bullbarbie.com",
            api_secret      = "",
            channel_id      = None,   # Discord channel ID for notifications
            staff_role_id   = None,   # Discord role ID that counts as support staff
            ticket_threads  = {},     # str(thread_id) -> int(wp_ticket_id)
            last_reply_ids  = {},     # str(wp_ticket_id) -> int(last_synced_reply_id)
            ticket_authors  = {},     # str(wp_ticket_id) -> {"discord_id": str, "wp_linked": bool}
        )
        self._creating_ticket: set = set()   # user IDs currently in ticket creation

    async def cog_load(self):
        self.session = aiohttp.ClientSession()
        self._sync_task = asyncio.create_task(self._sync_loop())

    async def cog_unload(self):
        if self._sync_task:
            self._sync_task.cancel()
        if self.session:
            await self.session.close()
            self.session = None

    # ── Internal helpers ──────────────────────────────────────────────────────

    async def _base(self) -> str:
        url = await self.config.wp_url()
        return url.rstrip("/") + "/wp-json/bb-support/v1"

    async def _headers(self) -> dict:
        secret = await self.config.api_secret()
        return {
            "X-BB-Support-Key": secret,
            "Content-Type": "application/json",
        }

    async def _get(self, path: str):
        """GET request. Returns parsed JSON or None on failure."""
        try:
            async with self.session.get(
                f"{await self._base()}{path}",
                headers=await self._headers(),
                timeout=aiohttp.ClientTimeout(total=10),
            ) as r:
                if r.status == 200:
                    return await r.json()
        except Exception:
            pass
        return None

    async def _post(self, path: str, data: dict):
        """POST request. Returns (json_body, status_code)."""
        try:
            async with self.session.post(
                f"{await self._base()}{path}",
                json=data,
                headers=await self._headers(),
                timeout=aiohttp.ClientTimeout(total=10),
            ) as r:
                return await r.json(), r.status
        except Exception as e:
            return {"error": str(e)}, 0

    async def _patch(self, path: str, data: dict):
        """PATCH request. Returns (json_body, status_code)."""
        try:
            async with self.session.patch(
                f"{await self._base()}{path}",
                json=data,
                headers=await self._headers(),
                timeout=aiohttp.ClientTimeout(total=10),
            ) as r:
                return await r.json(), r.status
        except Exception as e:
            return {"error": str(e)}, 0

    async def _is_staff(self, member: discord.Member) -> bool:
        """True if the member is an admin or has the configured staff role."""
        if member.guild_permissions.administrator:
            return True
        staff_role_id = await self.config.staff_role_id()
        if staff_role_id:
            return any(r.id == staff_role_id for r in member.roles)
        return False

    async def _ticket_id_for_thread(self, thread_id: int):
        """Return the WordPress ticket ID linked to this thread, or None."""
        threads = await self.config.ticket_threads()
        return threads.get(str(thread_id))

    async def _register_thread(self, thread_id: int, ticket_id: int):
        """Store the Discord thread → WordPress ticket mapping persistently."""
        async with self.config.ticket_threads() as threads:
            threads[str(thread_id)] = ticket_id

    async def _register_author(self, ticket_id: int, discord_id: str, wp_linked: bool):
        """Store the ticket creator's Discord ID and whether they have a linked WP account."""
        async with self.config.ticket_authors() as authors:
            authors[str(ticket_id)] = {"discord_id": discord_id, "wp_linked": wp_linked}

    async def _get_author(self, ticket_id: int) -> dict:
        """Return {discord_id, wp_linked} for a ticket, or try to resolve from WP."""
        authors = await self.config.ticket_authors()
        info = authors.get(str(ticket_id))
        if info:
            return info

        # Fallback: fetch ticket from WP and check if discord_user_id exists.
        ticket = await self._get(f"/tickets/{ticket_id}")
        if ticket and ticket.get("discord_user_id"):
            did = ticket["discord_user_id"]
            wp_linked = bool(ticket.get("wp_user_id"))
            await self._register_author(ticket_id, did, wp_linked)
            return {"discord_id": did, "wp_linked": wp_linked}
        return {"discord_id": "", "wp_linked": False}

    async def _wp_user_for(self, discord_id: str) -> dict:
        """Look up the linked WordPress user for a Discord user.

        Returns dict with user_id, email, display_name.
        Returns zeroed dict if no linked account.
        """
        data = await self._get(f"/user-by-discord/{discord_id}")
        if data and data.get("found"):
            return {
                "user_id":      data.get("user_id", 0),
                "email":        data.get("email", ""),
                "display_name": data.get("display_name", ""),
            }
        return {"user_id": 0, "email": "", "display_name": ""}

    def _topic_menu(self) -> str:
        return "\n".join(f"`{i}` — {label}" for i, (_, label) in enumerate(TOPICS, 1))

    _TOPIC_EMOJIS = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣"]

    def _topic_reaction_menu(self) -> str:
        return "\n".join(
            f"{self._TOPIC_EMOJIS[i]} — {label}"
            for i, (_, label) in enumerate(TOPICS)
        )

    def _ticket_embed(self, ticket: dict, prefix: str = "🎫 Ticket") -> discord.Embed:
        status = ticket.get("status", "open")
        embed  = discord.Embed(
            title = f"{prefix} #{ticket.get('id')} — {ticket.get('title', '(no title)')}",
            color = STATUS_COLORS.get(status, 0x2563EB),
        )
        embed.add_field(name="Status", value=status.capitalize(), inline=True)
        embed.add_field(name="Topic",  value=ticket.get("topic", "—").replace("_", " ").title(), inline=True)
        source = ticket.get("source", "web")
        embed.add_field(name="Source", value=source.capitalize(), inline=True)
        assigned = ticket.get("assigned_to", 0)
        embed.add_field(name="Assigned", value=f"User #{assigned}" if assigned else "Unassigned", inline=True)
        embed.set_footer(text="Trading Ranch Support · Reply in this thread to respond")
        return embed

    # ── !support — directs users to the support channel ────────────────────

    @commands.command(name="support")
    @commands.guild_only()
    async def support(self, ctx: commands.Context):
        """Points users to the support channel where tickets are auto-created."""
        channel_id = await self.config.channel_id()
        if channel_id and ctx.guild:
            channel = ctx.guild.get_channel(channel_id)
            if channel:
                await ctx.send(
                    f"To open a support ticket, head to {channel.mention} "
                    f"and describe your issue. A **private thread** will be "
                    f"created for you automatically.",
                    delete_after=20,
                )
                return
        await ctx.send(
            "Support channel hasn't been set up yet. "
            "Ask an admin to run `[p]trsupport setchannel #channel`."
        )

    # ── Auto-ticket creation from support channel messages ───────────────────

    async def _create_ticket_from_message(self, message: discord.Message):
        """Delete the user's message, create a WP ticket, open a private thread."""
        content = message.content.strip()
        if len(content) < 10:
            try:
                await message.channel.send(
                    f"{message.author.mention} Please provide a bit more detail "
                    f"(at least a sentence) so we can help you effectively.",
                    delete_after=10,
                )
            except discord.HTTPException:
                pass
            return

        author     = message.author
        discord_id = str(author.id)

        # Prevent duplicate tickets from rapid messages.
        if author.id in self._creating_ticket:
            return
        self._creating_ticket.add(author.id)

        try:
            # Delete the original message to keep the channel tidy.
            try:
                await message.delete()
            except (discord.Forbidden, discord.HTTPException):
                pass

            # Check for a linked WordPress account.
            wp_info    = await self._wp_user_for(discord_id)
            wp_user_id = wp_info["user_id"]
            user_email = wp_info["email"]

            # Create the ticket on WordPress (topic defaults to "general").
            result, status_code = await self._post("/tickets", {
                "topic":            "general",
                "message":          content,
                "name":             author.display_name,
                "email":            user_email,
                "source":           "discord",
                "discord_user_id":  discord_id,
                "discord_username": str(author),
                "wp_user_id":       wp_user_id,
            })

            if status_code not in (200, 201) or not result.get("success"):
                try:
                    await message.channel.send(
                        f"{author.mention} ❌ Failed to create a ticket. "
                        f"Please try again or contact staff directly.",
                        delete_after=15,
                    )
                except discord.HTTPException:
                    pass
                return

            ticket    = result.get("ticket", {})
            ticket_id = result.get("ticket_id")

            if not ticket_id:
                try:
                    await message.channel.send(
                        f"{author.mention} ❌ Ticket submitted but no ID returned. "
                        f"Please contact staff.",
                        delete_after=15,
                    )
                except discord.HTTPException:
                    pass
                return

            # Create a private thread — only invited users and members with
            # Manage Threads permission can see it.
            try:
                thread = await message.channel.create_thread(
                    name                  = f"Ticket #{ticket_id} — {author.display_name}",
                    type                  = discord.ChannelType.private_thread,
                    auto_archive_duration = 10080,
                )
            except discord.Forbidden:
                try:
                    await message.channel.send(
                        f"{author.mention} ❌ I don't have permission to create "
                        f"private threads here. Please contact an admin.",
                        delete_after=15,
                    )
                except discord.HTTPException:
                    pass
                return
            except discord.HTTPException as exc:
                try:
                    await message.channel.send(
                        f"{author.mention} ❌ Thread creation failed: {exc}",
                        delete_after=15,
                    )
                except discord.HTTPException:
                    pass
                return

            # Invite the ticket author into the private thread.
            try:
                await thread.add_user(author)
            except discord.HTTPException:
                pass

            # Persist mappings.
            await self._register_thread(thread.id, ticket_id)
            await self._register_author(ticket_id, discord_id, bool(wp_user_id))
            await self._patch(f"/tickets/{ticket_id}", {
                "discord_thread_id": str(thread.id),
            })

            # Post ticket embed and the user's original message.
            embed = self._ticket_embed(ticket, prefix="🆕 New Ticket")
            embed.add_field(name="From", value=f"{author.mention} ({author})", inline=False)
            if wp_user_id:
                embed.add_field(
                    name="WP Account", value=f"Linked (ID {wp_user_id})", inline=True,
                )
            await thread.send(embed=embed)
            await thread.send(f"**{author.display_name}:**\n\n{content}")

            # Welcome message — topic is fixed to "general".
            await thread.send(
                f"Thanks for reaching out, {author.mention}! A support team member "
                f"will be with you shortly."
            )

            # Notify the staff role inside the thread.
            staff_role_id = await self.config.staff_role_id()
            if staff_role_id:
                await thread.send(f"<@&{staff_role_id}>")

            # Auto-deleting confirmation in the support channel.
            try:
                await message.channel.send(
                    f"🎫 {author.mention}, your support ticket **#{ticket_id}** "
                    f"has been created. Continue in {thread.mention}.",
                    delete_after=20,
                )
            except discord.HTTPException:
                pass

        finally:
            self._creating_ticket.discard(author.id)

    # ── on_message — support channel + thread relay ──────────────────────────

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """
        1. Support channel — auto-create a private ticket thread.
        2. Ticket thread  — relay messages to WordPress as replies.
        """
        if message.author.bot:
            return
        if not message.guild:
            return

        # ── Support channel: auto-create ticket ─────────────────────────────
        channel_id = await self.config.channel_id()
        if (
            channel_id
            and message.channel.id == channel_id
            and not isinstance(message.channel, discord.Thread)
        ):
            prefixes = await self.bot.get_valid_prefixes(message.guild)
            if any(message.content.startswith(p) for p in prefixes):
                return
            await self._create_ticket_from_message(message)
            return

        # ── Ticket thread: relay to WordPress ────────────────────────────────
        if not isinstance(message.channel, discord.Thread):
            return

        ticket_id = await self._ticket_id_for_thread(message.channel.id)
        if not ticket_id:
            return

        prefixes = await self.bot.get_valid_prefixes(message.guild)
        if any(message.content.startswith(p) for p in prefixes):
            return

        is_staff    = await self._is_staff(message.author)
        is_internal = False
        content     = message.content.strip()

        if is_staff and content.lower().startswith("[note]"):
            is_internal = True
            content     = content[6:].strip()

        if not content:
            return

        wp_info    = await self._wp_user_for(str(message.author.id))
        wp_user_id = wp_info["user_id"]

        result, status_code = await self._post(f"/tickets/{ticket_id}/replies", {
            "message":          content,
            "name":             message.author.display_name,
            "discord_user_id":  str(message.author.id),
            "discord_username": str(message.author),
            "wp_user_id":       wp_user_id,
            "is_staff":         is_staff,
            "is_internal":      is_internal,
        })

        if status_code in (200, 201) and result.get("reply_id"):
            async with self.config.last_reply_ids() as ids:
                current = ids.get(str(ticket_id), 0)
                new_id  = result["reply_id"]
                if new_id > current:
                    ids[str(ticket_id)] = new_id

        try:
            if status_code in (200, 201):
                await message.add_reaction("✅")
            else:
                await message.add_reaction("❌")
        except (discord.Forbidden, discord.HTTPException):
            pass

    # ── WP → Discord reply sync ──────────────────────────────────────────────

    async def _sync_loop(self):
        """Background loop: poll WordPress for new web replies and post them to Discord threads."""
        await self.bot.wait_until_ready()
        while True:
            try:
                await self._sync_wp_replies()
            except asyncio.CancelledError:
                return
            except Exception:
                log.exception("Error in WP→Discord sync loop")
            await asyncio.sleep(SYNC_INTERVAL)

    async def _sync_wp_replies(self):
        """Check all tracked ticket threads for new web-originated replies."""
        # Bail out early if API isn't configured.
        secret = await self.config.api_secret()
        if not secret:
            return

        threads  = await self.config.ticket_threads()
        last_ids = await self.config.last_reply_ids()
        if not threads:
            return

        for thread_id_str, ticket_id in threads.items():
            thread_id = int(thread_id_str)

            # Resolve the Discord thread.
            thread = self.bot.get_channel(thread_id)
            if not thread or not isinstance(thread, discord.Thread):
                continue
            if thread.archived or thread.locked:
                continue

            # Fetch replies from WP.
            replies = await self._get(f"/tickets/{ticket_id}/replies")
            if not replies or not isinstance(replies, list):
                continue

            last_synced = last_ids.get(str(ticket_id), 0)
            max_id      = last_synced

            for r in replies:
                reply_id = r.get("id", 0)
                if reply_id <= last_synced:
                    continue
                if reply_id > max_id:
                    max_id = reply_id

                # Only sync web-originated, non-internal replies to the thread.
                if r.get("source") == "discord":
                    continue
                if r.get("is_internal"):
                    continue

                author_name = r.get("author_name", "Unknown")
                msg_text    = r.get("message", "")
                if not msg_text:
                    continue

                # Build the reply message.
                reply_text = f"🌐 **{author_name}** (via website):\n\n{msg_text}"

                # Mention discord-only users so they get a notification.
                # Linked WP users get email via WP — no need to ping.
                author_info = await self._get_author(ticket_id)
                if author_info["discord_id"] and not author_info["wp_linked"]:
                    reply_text += f"\n\n<@{author_info['discord_id']}>"

                try:
                    await thread.send(reply_text)
                except (discord.Forbidden, discord.HTTPException):
                    log.warning("Could not post WP reply to thread %s", thread_id)

            # Persist high-water mark.
            if max_id > last_synced:
                async with self.config.last_reply_ids() as ids:
                    ids[str(ticket_id)] = max_id

    # ── Admin / staff commands ────────────────────────────────────────────────

    @commands.group(name="trsupport", aliases=["trs"])
    @commands.guild_only()
    async def trsupport(self, ctx: commands.Context):
        """Trading Ranch support ticket administration."""
        if ctx.invoked_subcommand is None:
            await ctx.send_help(self.trsupport)

    @trsupport.command(name="setchannel")
    @commands.admin_or_permissions(manage_guild=True)
    async def trs_setchannel(self, ctx: commands.Context, channel: discord.TextChannel):
        """Set the channel where new ticket notifications are posted."""
        await self.config.channel_id.set(channel.id)
        await ctx.send(f"✅ Notification channel set to {channel.mention}.")

    @trsupport.command(name="setsecret")
    @commands.admin_or_permissions(manage_guild=True)
    async def trs_setsecret(self, ctx: commands.Context, secret: str):
        """Set the WordPress API secret (from WP Admin → TR Support → Settings).

        Run this in a private channel or DM — the command message will be deleted."""
        await self.config.api_secret.set(secret)
        try:
            await ctx.message.delete()
        except Exception:
            pass
        await ctx.send("✅ API secret saved. Your message was deleted for security.")

    @trsupport.command(name="seturl")
    @commands.admin_or_permissions(manage_guild=True)
    async def trs_seturl(self, ctx: commands.Context, url: str):
        """Set the WordPress site URL (default: https://bullbarbie.com)."""
        await self.config.wp_url.set(url.rstrip("/"))
        await ctx.send(f"✅ WordPress URL set to `{url.rstrip('/')}`.")

    @trsupport.command(name="setstaffrole")
    @commands.admin_or_permissions(manage_guild=True)
    async def trs_setstaffrole(self, ctx: commands.Context, role: discord.Role):
        """Set the Discord role that counts as support staff.
        Staff can update ticket statuses and post internal notes in threads."""
        await self.config.staff_role_id.set(role.id)
        await ctx.send(f"✅ Staff role set to **{role.name}**.")

    @trsupport.command(name="instructions")
    @commands.admin_or_permissions(manage_guild=True)
    async def trs_instructions(self, ctx: commands.Context):
        """Post a welcome / how-to embed in the support channel."""
        channel_id = await self.config.channel_id()
        if not channel_id:
            await ctx.send("❌ Set a support channel first with `[p]trsupport setchannel`.")
            return
        channel = ctx.guild.get_channel(channel_id) if ctx.guild else None
        if not channel:
            await ctx.send("❌ Could not find the configured support channel.")
            return
        embed = discord.Embed(
            title="🎫 Trading Ranch Support",
            description=(
                "Need help? Just **type your issue in this channel** and a private "
                "support ticket will be created for you automatically.\n\n"
                "**How it works:**\n"
                "1️⃣ Describe your issue in a message below\n"
                "2️⃣ Your message is removed and a **private thread** is created\n"
                "3️⃣ Our support team will be in touch in your private ticket thread\n\n"
                "Only **you** and the support team can see your thread.\n\n"
                "You can also open a ticket on our website at "
                "[bullbarbie.com](https://bullbarbie.com)."
            ),
            color=0x1a0a2e,
        )
        embed.set_footer(text="Trading Ranch Support System")
        await channel.send(embed=embed)
        if ctx.channel.id != channel_id:
            await ctx.send(f"✅ Instructions posted in {channel.mention}.")

    @trsupport.command(name="ping")
    @commands.admin_or_permissions(manage_guild=True)
    async def trs_ping(self, ctx: commands.Context):
        """Test the connection to the WordPress REST API."""
        async with ctx.typing():
            data = await self._get("/tickets?status=open")
        if data is not None:
            count = len(data) if isinstance(data, list) else 0
            await ctx.send(f"✅ Connected to WordPress. {count} open ticket(s) found.")
        else:
            await ctx.send(
                "❌ Could not reach the WordPress REST API. "
                "Check `[p]trsupport seturl` and `[p]trsupport setsecret`."
            )

    @trsupport.command(name="view")
    @commands.admin_or_permissions(manage_guild=True)
    async def trs_view(self, ctx: commands.Context, ticket_id: int):
        """View a ticket summary by ID."""
        async with ctx.typing():
            ticket = await self._get(f"/tickets/{ticket_id}")
        if not ticket:
            await ctx.send(f"❌ Could not find ticket #{ticket_id}.")
            return
        embed = self._ticket_embed(ticket)
        if ticket.get("discord_username"):
            embed.add_field(name="Discord", value=ticket["discord_username"], inline=True)
        if ticket.get("guest_email"):
            embed.add_field(name="Guest Email", value=ticket["guest_email"], inline=True)
        if ticket.get("discord_thread_id"):
            embed.add_field(name="Thread", value=f"<#{ticket['discord_thread_id']}>", inline=True)
        await ctx.send(embed=embed)

    @trsupport.command(name="status")
    @commands.admin_or_permissions(manage_guild=True)
    async def trs_setstatus(self, ctx: commands.Context, ticket_id: int, new_status: str):
        """Update a ticket's status.  Valid values: open, pending, resolved, closed."""
        new_status = new_status.lower()
        if new_status not in STATUSES:
            await ctx.send(f"Invalid status. Choose from: `{'`, `'.join(STATUSES)}`")
            return
        result, code = await self._patch(f"/tickets/{ticket_id}", {"status": new_status})
        if code == 200:
            await ctx.send(f"✅ Ticket #{ticket_id} status updated to **{new_status}**.")
        else:
            await ctx.send(f"❌ Failed to update ticket #{ticket_id}.")

    @trsupport.command(name="close")
    @commands.admin_or_permissions(manage_guild=True)
    async def trs_close(self, ctx: commands.Context, ticket_id: int):
        """Close a ticket."""
        result, code = await self._patch(f"/tickets/{ticket_id}", {"status": "closed"})
        if code == 200:
            await ctx.send(f"✅ Ticket #{ticket_id} has been closed.")
        else:
            await ctx.send(f"❌ Could not close ticket #{ticket_id}.")

    @trsupport.command(name="claim")
    @commands.admin_or_permissions(manage_guild=True)
    async def trs_claim(self, ctx: commands.Context, ticket_id: int):
        """Claim a ticket — assigns it to your linked WordPress account."""
        wp_info = await self._wp_user_for(str(ctx.author.id))
        if not wp_info["user_id"]:
            await ctx.send(
                "❌ Your Discord account isn't linked to a WordPress account. "
                "Link your account on the Trading Ranch website first."
            )
            return
        result, code = await self._patch(f"/tickets/{ticket_id}", {
            "assigned_to": wp_info["user_id"],
        })
        if code == 200:
            await ctx.send(
                f"✅ Ticket #{ticket_id} assigned to you "
                f"({wp_info['display_name'] or ctx.author.display_name})."
            )
        else:
            await ctx.send(f"❌ Could not claim ticket #{ticket_id}.")

    @trsupport.command(name="reply")
    @commands.admin_or_permissions(manage_guild=True)
    async def trs_reply(self, ctx: commands.Context, ticket_id: int, *, message: str):
        """Reply to a ticket directly (without needing to be in its thread)."""
        wp_info    = await self._wp_user_for(str(ctx.author.id))
        wp_user_id = wp_info["user_id"]
        is_staff   = await self._is_staff(ctx.author)

        result, status_code = await self._post(f"/tickets/{ticket_id}/replies", {
            "message":          message,
            "name":             ctx.author.display_name,
            "discord_user_id":  str(ctx.author.id),
            "discord_username": str(ctx.author),
            "wp_user_id":       wp_user_id,
            "is_staff":         is_staff,
            "is_internal":      False,
        })

        if status_code in (200, 201):
            await ctx.send(f"✅ Reply sent to ticket #{ticket_id}.")

            # Track reply ID for the sync loop.
            if result.get("reply_id"):
                async with self.config.last_reply_ids() as ids:
                    current = ids.get(str(ticket_id), 0)
                    if result["reply_id"] > current:
                        ids[str(ticket_id)] = result["reply_id"]

            # Also forward to the linked Discord thread if one exists.
            threads = await self.config.ticket_threads()
            for tid_str, tid in threads.items():
                if tid == ticket_id:
                    thread = self.bot.get_channel(int(tid_str))
                    if thread and isinstance(thread, discord.Thread):
                        try:
                            reply_text = (
                                f"**{ctx.author.display_name}** (via `!trs reply`):\n\n{message}"
                            )
                            # Tag discord-only ticket creator.
                            author_info = await self._get_author(ticket_id)
                            if author_info["discord_id"] and not author_info["wp_linked"]:
                                reply_text += f"\n\n<@{author_info['discord_id']}>"
                            await thread.send(reply_text)
                        except (discord.Forbidden, discord.HTTPException):
                            pass
                    break
        else:
            await ctx.send(f"❌ Failed to reply to ticket #{ticket_id}.")

    @trsupport.command(name="list")
    @commands.admin_or_permissions(manage_guild=True)
    async def trs_list(self, ctx: commands.Context, status: str = "open"):
        """List tickets. Filter by status: open, pending, resolved, closed, or all."""
        status = status.lower()
        if status not in STATUSES and status != "all":
            await ctx.send(f"Invalid status. Choose from: `all`, `{'`, `'.join(STATUSES)}`")
            return

        async with ctx.typing():
            path = "/tickets" if status == "all" else f"/tickets?status={status}"
            data = await self._get(path)

        if data is None:
            await ctx.send("❌ Could not fetch tickets from WordPress.")
            return

        if not isinstance(data, list) or not data:
            await ctx.send(f"No **{status}** tickets found.")
            return

        embed = discord.Embed(
            title=f"📋 Tickets — {status.capitalize()}" if status != "all" else "📋 All Tickets",
            color=STATUS_COLORS.get(status, 0x2563EB),
        )
        for t in data[:20]:
            tid    = t.get("id", "?")
            title  = t.get("title", "(no title)")
            st     = t.get("status", "open")
            source = t.get("source", "web")
            embed.add_field(
                name=f"#{tid} — {title}",
                value=f"Status: **{st}** · Source: {source}",
                inline=False,
            )
        if len(data) > 20:
            embed.set_footer(text=f"Showing 20 of {len(data)} tickets.")
        else:
            embed.set_footer(text=f"{len(data)} ticket(s) found.")

        await ctx.send(embed=embed)

    @trsupport.command(name="settings")
    @commands.admin_or_permissions(manage_guild=True)
    async def trs_settings(self, ctx: commands.Context):
        """Show current configuration (secret is hidden)."""
        wp_url        = await self.config.wp_url()
        api_secret    = await self.config.api_secret()
        channel_id    = await self.config.channel_id()
        staff_role_id = await self.config.staff_role_id()

        channel    = ctx.guild.get_channel(channel_id) if channel_id and ctx.guild else None
        staff_role = ctx.guild.get_role(staff_role_id) if staff_role_id and ctx.guild else None
        threads    = await self.config.ticket_threads()

        embed = discord.Embed(title="BB TR Support — Settings", color=0x1a0a2e)
        embed.add_field(name="WordPress URL",  value=f"`{wp_url}`",                                inline=False)
        embed.add_field(name="API Secret",     value="✅ Set" if api_secret else "❌ Not set",     inline=True)
        embed.add_field(name="Notif. Channel", value=channel.mention if channel else "❌ Not set", inline=True)
        embed.add_field(name="Staff Role",     value=staff_role.mention if staff_role else "❌ Not set", inline=True)
        embed.add_field(name="Active Threads", value=str(len(threads)),                            inline=True)
        embed.add_field(name="WP Sync",        value=f"Every {SYNC_INTERVAL}s",                    inline=True)
        await ctx.send(embed=embed)
