"""
BB TR Support — Red-bot cog
Bridges Discord to the Trading Ranch WordPress support ticket system.

Users type in the support channel; their message is deleted and a **private
thread** is created automatically.  A notification with **Claim**, **Close**,
and **Resolved** buttons is posted in the staff notification channel.
Staff click Claim to be added to the thread.  Web-originated tickets also
get a Discord thread and staff notification automatically.

Commands:
  !support                                  — points users to the support channel
  !trsupport setchannel #channel            — set support channel              (admin)
  !trsupport setnotifychannel #channel      — set staff notification channel   (admin)
  !trsupport setstaffrole @role             — set support staff role           (admin)
  !trsupport setsecret <key>                — set WordPress API secret         (admin)
  !trsupport seturl <url>                   — set WordPress site URL           (admin)
  !trsupport settitle <text>                — change instructions embed title  (admin)
  !trsupport instructions                   — post welcome embed in channel    (admin)
  !trsupport settings                       — show current configuration       (admin)
  !trsupport status <id> <status>           — update a ticket's status        (staff)
  !trsupport view <id>                      — view a ticket summary            (staff)
  !trsupport close <id>                     — close a ticket                   (staff)
  !trsupport claim <id>                     — claim / assign a ticket to you   (staff)
  !trsupport reply <id> <msg>               — reply to a ticket directly       (staff)
  !trsupport list [status]                  — list tickets                     (staff)
  !trsupport ping                           — test connection to WordPress     (staff)

Thread relay:
  Any message posted in a ticket thread is forwarded to WordPress as a reply.
  Staff can prefix a message with [note] to mark it as an internal note.
  Web replies are automatically synced back to the Discord thread.
"""

import asyncio
import logging
import re
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


# ── Persistent ticket notification view ──────────────────────────────────────


class TicketView(discord.ui.View):
    """Persistent Claim / Close / Resolved buttons on ticket notifications.

    ``custom_id`` encodes the ticket and thread so the view survives bot
    restarts.  Multiple staff can click **Claim** — each is added to the
    thread and the embed records who has claimed it.
    """

    def __init__(self, cog: "BBTRSupport" = None):
        super().__init__(timeout=None)
        self.cog = cog

    # ── helper: resolve cog at interaction time (persistent views) ────────
    def _get_cog(self, interaction: discord.Interaction) -> "BBTRSupport":
        if self.cog:
            return self.cog
        return interaction.client.get_cog("BBTRSupport")

    async def _update_notification(
        self,
        interaction: discord.Interaction,
        ticket_id: int,
        thread_id: int,
        *,
        new_status: str | None = None,
        claimer: discord.Member | None = None,
    ):
        """Re-build the notification embed and edit the original message."""
        cog = self._get_cog(interaction)
        if not cog:
            return

        msg = interaction.message
        old_embed = msg.embeds[0] if msg.embeds else None
        if not old_embed:
            return

        # Rebuild embed preserving existing fields.
        embed = discord.Embed(
            title=old_embed.title,
            color=STATUS_COLORS.get(new_status, old_embed.color.value if old_embed.color else 0x2563EB),
            description=old_embed.description or "",
        )

        # Copy existing fields, updating Status and Claimed By as needed.
        claimed_field_value = None
        for field in old_embed.fields:
            if field.name == "Status" and new_status:
                embed.add_field(name="Status", value=new_status.capitalize(), inline=field.inline)
            elif field.name == "Claimed By":
                # Append new claimer.
                existing = field.value or ""
                if claimer and claimer.mention not in existing:
                    claimed_field_value = f"{existing}, {claimer.mention}" if existing else claimer.mention
                else:
                    claimed_field_value = existing
                embed.add_field(name="Claimed By", value=claimed_field_value, inline=field.inline)
            else:
                embed.add_field(name=field.name, value=field.value, inline=field.inline)

        # If claimer provided but no Claimed By field existed yet, add it.
        if claimer and claimed_field_value is None:
            embed.add_field(name="Claimed By", value=claimer.mention, inline=True)

        if old_embed.footer:
            embed.set_footer(text=old_embed.footer.text)

        # Disable buttons if ticket is now closed/resolved.
        view = self if new_status not in ("closed", "resolved") else None
        try:
            await interaction.message.edit(embed=embed, view=view)
        except (discord.Forbidden, discord.HTTPException):
            pass

    # ── Claim button ─────────────────────────────────────────────────────
    @discord.ui.button(
        label="Claim",
        style=discord.ButtonStyle.success,
        custom_id="trs:claim",
        emoji="🙋",
    )
    async def btn_claim(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog = self._get_cog(interaction)
        if not cog:
            await interaction.response.send_message("❌ Cog not loaded.", ephemeral=True)
            return

        # Permission check.
        if not await cog._is_staff(interaction.user):
            await interaction.response.send_message("❌ Only support staff can claim tickets.", ephemeral=True)
            return

        # Extract ticket_id and thread_id from the embed.
        ticket_id, thread_id = self._ids_from_embed(interaction.message)
        if not ticket_id:
            await interaction.response.send_message("❌ Could not determine ticket info.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        # Add the staff member to the private thread.
        thread = interaction.guild.get_channel_or_thread(thread_id) if thread_id else None
        if not thread:
            # Try fetching if not cached.
            try:
                thread = await interaction.guild.fetch_channel(thread_id)
            except Exception:
                thread = None
        if thread and isinstance(thread, discord.Thread):
            try:
                await thread.add_user(interaction.user)
            except discord.HTTPException:
                pass

        # Assign on WordPress.
        wp_info = await cog._wp_user_for(str(interaction.user.id))
        if wp_info["user_id"]:
            await cog._patch(f"/tickets/{ticket_id}", {"assigned_to": wp_info["user_id"]})

        await self._update_notification(interaction, ticket_id, thread_id, claimer=interaction.user)
        thread_mention = f"<#{thread_id}>" if thread_id else "the ticket thread"
        await interaction.followup.send(
            f"✅ You've claimed ticket #{ticket_id}. Head to {thread_mention}.",
            ephemeral=True,
        )

    # ── Close button ─────────────────────────────────────────────────────
    @discord.ui.button(
        label="Close",
        style=discord.ButtonStyle.danger,
        custom_id="trs:close",
        emoji="🔒",
    )
    async def btn_close(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog = self._get_cog(interaction)
        if not cog:
            await interaction.response.send_message("❌ Cog not loaded.", ephemeral=True)
            return
        if not await cog._is_staff(interaction.user):
            await interaction.response.send_message("❌ Only support staff can close tickets.", ephemeral=True)
            return

        ticket_id, thread_id = self._ids_from_embed(interaction.message)
        if not ticket_id:
            await interaction.response.send_message("❌ Could not determine ticket info.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        result, code = await cog._patch(f"/tickets/{ticket_id}", {"status": "closed"})
        if code == 200:
            await cog._archive_thread_for_ticket(ticket_id)
            await self._update_notification(interaction, ticket_id, thread_id, new_status="closed")
            await interaction.followup.send(f"✅ Ticket #{ticket_id} closed.", ephemeral=True)
        else:
            await interaction.followup.send(f"❌ Failed to close ticket #{ticket_id}.", ephemeral=True)

    # ── Resolved button ──────────────────────────────────────────────────
    @discord.ui.button(
        label="Resolved",
        style=discord.ButtonStyle.secondary,
        custom_id="trs:resolved",
        emoji="✅",
    )
    async def btn_resolved(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog = self._get_cog(interaction)
        if not cog:
            await interaction.response.send_message("❌ Cog not loaded.", ephemeral=True)
            return
        if not await cog._is_staff(interaction.user):
            await interaction.response.send_message("❌ Only support staff can resolve tickets.", ephemeral=True)
            return

        ticket_id, thread_id = self._ids_from_embed(interaction.message)
        if not ticket_id:
            await interaction.response.send_message("❌ Could not determine ticket info.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        result, code = await cog._patch(f"/tickets/{ticket_id}", {"status": "resolved"})
        if code == 200:
            await cog._archive_thread_for_ticket(ticket_id)
            await self._update_notification(interaction, ticket_id, thread_id, new_status="resolved")
            await interaction.followup.send(f"✅ Ticket #{ticket_id} resolved.", ephemeral=True)
        else:
            await interaction.followup.send(f"❌ Failed to resolve ticket #{ticket_id}.", ephemeral=True)

    # ── helpers ──────────────────────────────────────────────────────────
    @staticmethod
    def _ids_from_embed(message: discord.Message) -> tuple:
        """Extract (ticket_id, thread_id) from the notification embed."""
        if not message.embeds:
            return None, None
        embed = message.embeds[0]
        title = embed.title or ""
        # Title format: "🎫 Ticket #123 — Author" or "🌐 Web Ticket #123 — Author"
        m = re.search(r"#(\d+)", title)
        ticket_id = int(m.group(1)) if m else None
        # Thread link is stored in a field named "Thread".
        thread_id = None
        for field in embed.fields:
            if field.name == "Thread":
                ch_match = re.search(r"<#(\d+)>", field.value or "")
                if ch_match:
                    thread_id = int(ch_match.group(1))
                break
        return ticket_id, thread_id


# ── "Open a Ticket" button + modal for the instructions embed ─────────────────


class TicketCreateModal(discord.ui.Modal, title="Open a Support Ticket"):
    """Modal that pops up when a user clicks the Open a Ticket button."""

    description = discord.ui.TextInput(
        label="Describe your issue",
        style=discord.TextStyle.paragraph,
        placeholder="Tell us what you need help with (at least a sentence)…",
        min_length=10,
        max_length=2000,
    )

    async def on_submit(self, interaction: discord.Interaction):
        cog = interaction.client.get_cog("BBTRSupport")
        if not cog:
            await interaction.response.send_message("❌ Support system not loaded.", ephemeral=True)
            return

        author     = interaction.user
        discord_id = str(author.id)
        content    = self.description.value.strip()

        # Prevent duplicate tickets from rapid clicks.
        if author.id in cog._creating_ticket:
            await interaction.response.send_message(
                "⏳ Your ticket is already being created, please wait…", ephemeral=True
            )
            return
        cog._creating_ticket.add(author.id)

        # Defer so we have time to hit the WP API + create a thread.
        await interaction.response.defer(ephemeral=True)

        try:
            wp_info    = await cog._wp_user_for(discord_id)
            wp_user_id = wp_info["user_id"]
            user_email = wp_info["email"]

            result, status_code = await cog._post("/tickets", {
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
                await interaction.followup.send(
                    "❌ Failed to create a ticket. Please try again or contact staff directly.",
                    ephemeral=True,
                )
                return

            ticket    = result.get("ticket", {})
            ticket_id = result.get("ticket_id")
            if not ticket_id:
                await interaction.followup.send(
                    "❌ Ticket submitted but no ID returned. Please contact staff.",
                    ephemeral=True,
                )
                return

            # Create the private thread.
            channel = interaction.channel
            try:
                thread = await channel.create_thread(
                    name=f"Ticket #{ticket_id} — {author.display_name}",
                    type=discord.ChannelType.private_thread,
                    auto_archive_duration=10080,
                )
            except (discord.Forbidden, discord.HTTPException) as exc:
                await interaction.followup.send(
                    f"❌ Thread creation failed: {exc}", ephemeral=True
                )
                return

            try:
                await thread.add_user(author)
            except discord.HTTPException:
                pass

            await cog._register_thread(thread.id, ticket_id)
            await cog._register_author(ticket_id, discord_id, bool(wp_user_id))
            await cog._patch(f"/tickets/{ticket_id}", {"discord_thread_id": str(thread.id)})

            # Post embed + user message inside the thread.
            embed = cog._ticket_embed(ticket, prefix="🆕 New Ticket")
            embed.add_field(name="From", value=f"{author.mention} ({author})", inline=False)
            if wp_user_id:
                wp_display = wp_info.get("display_name") or str(wp_user_id)
                embed.add_field(name="Web Account", value=wp_display, inline=True)
            embed_msg = await thread.send(embed=embed)
            await thread.send(f"**{author.display_name}:**\n\n{content}")
            await thread.send(
                f"Thanks for reaching out, {author.mention}! A support team member "
                f"will be with you shortly."
            )

            # Notification with buttons.
            await cog._post_ticket_notification(
                ticket, thread,
                source_label="🆕 New Ticket",
                submitter=f"{author.mention} ({author})",
                guild=interaction.guild,
            )

            # Topic selection via emoji reactions.
            topic_msg = await thread.send(cog._topic_reaction_menu())
            for emoji in cog._TOPIC_EMOJIS:
                try:
                    await topic_msg.add_reaction(emoji)
                except discord.HTTPException:
                    pass
            cog._topic_select[topic_msg.id] = {
                "ticket_id":    ticket_id,
                "thread_id":    thread.id,
                "author_id":    author.id,
                "embed_msg_id": embed_msg.id,
            }

            # Ephemeral confirmation — only the user sees this.
            await interaction.followup.send(
                f"🎫 Your support ticket **#{ticket_id}** has been created! "
                f"Continue in {thread.mention}.",
                ephemeral=True,
            )
        finally:
            cog._creating_ticket.discard(author.id)


class TicketCreateView(discord.ui.View):
    """Persistent view attached to the instructions embed in the support channel."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Open a Ticket",
        style=discord.ButtonStyle.primary,
        custom_id="trs:open_ticket",
        emoji="🎫",
    )
    async def btn_open_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(TicketCreateModal())


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
            # Instructions embed customisation
            instr_title       = "🎫 Trading Ranch Support",
            instr_description = (
                "Need help? Just type your message here and a private support ticket "
                "will be created for you.\n\n"
                "How it works:\n"
                "1️⃣ Describe your issue in a message below\n"
                "2️⃣ A private ticket thread is created\n"
                "3️⃣ Chat directly with our staff privately\n\n"
                "Only you and the support team can see your thread. "
                "Include details and screenshots when applicable.\n\n"
                "Support tickets can also be opened on our website at "
                "[bullbarbie.com](https://bullbarbie.com), but please do not submit "
                "duplicate tickets as this slows us down."
            ),
            instr_footer      = "Trading Ranch Support System",
            instr_color       = 0x1a0a2e,
            # Staff notification / log channel
            notify_channel_id       = None,   # separate channel for staff alerts
            last_notified_ticket_id = 0,      # highest web-ticket ID already alerted
        )
        self._creating_ticket: set = set()   # user IDs currently in ticket creation
        # In-memory: message_id → topic-selection context (reset on reload)
        self._topic_select: dict = {}

    async def cog_load(self):
        self.session = aiohttp.ClientSession()
        self._sync_task = asyncio.create_task(self._sync_loop())
        # Register persistent button views so they work after restarts.
        self.bot.add_view(TicketView(cog=self))
        self.bot.add_view(TicketCreateView())

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

    async def _resolve_ticket_id(
        self, ctx: commands.Context, ticket_id: int | None
    ) -> int | None:
        """Return ticket_id if provided, else auto-detect from the current thread."""
        if ticket_id is not None:
            return ticket_id
        if isinstance(ctx.channel, discord.Thread):
            tid = await self._ticket_id_for_thread(ctx.channel.id)
            if tid:
                return tid
        await ctx.send(
            "❌ Provide a ticket ID, or run this command inside a ticket thread."
        )
        return None

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

    async def _clean_discord_text(self, content: str, guild: discord.Guild) -> str:
        """Resolve Discord mentions, channels, and roles to plain-text names.

        <@123> / <@!123>  →  @DisplayName
        <#123>            →  #channel-name
        <@&123>           →  @RoleName
        """
        def _resolve_user(m):
            uid = int(m.group(1))
            member = guild.get_member(uid)
            return f"@{member.display_name}" if member else m.group(0)

        def _resolve_channel(m):
            cid = int(m.group(1))
            ch = guild.get_channel(cid)
            return f"#{ch.name}" if ch else m.group(0)

        def _resolve_role(m):
            rid = int(m.group(1))
            role = guild.get_role(rid)
            return f"@{role.name}" if role else m.group(0)

        content = re.sub(r'<@!?(\d+)>', _resolve_user, content)
        content = re.sub(r'<#(\d+)>',   _resolve_channel, content)
        content = re.sub(r'<@&(\d+)>',  _resolve_role, content)
        return content

    @staticmethod
    def _collect_image_urls(message: discord.Message) -> list:
        """Return a list of image URLs from attachments and embeds."""
        urls = []
        for att in message.attachments:
            if att.content_type and att.content_type.startswith("image/"):
                urls.append(att.url)
            elif att.filename and att.filename.lower().rsplit(".", 1)[-1] in ("png", "jpg", "jpeg", "gif", "webp"):
                urls.append(att.url)
        # Also pick up image embeds auto-generated from pasted URLs.
        for emb in message.embeds:
            if emb.type == "image" and emb.url:
                urls.append(emb.url)
        return urls

    def _topic_menu(self) -> str:
        return "\n".join(f"`{i}` — {label}" for i, (_, label) in enumerate(TOPICS, 1))

    _TOPIC_EMOJIS = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣"]

    def _topic_reaction_menu(self) -> str:
        parts = [
            f"{self._TOPIC_EMOJIS[i]} {label.split()[0]}"
            for i, (_, label) in enumerate(TOPICS)
        ]
        return "React below to select a topic (Optional):\n" + " | ".join(parts)

    def _ticket_embed(self, ticket: dict, prefix: str = "🎫 Ticket") -> discord.Embed:
        status = ticket.get("status", "open")
        embed  = discord.Embed(
            title = f"{prefix} #{ticket.get('id')} — {ticket.get('title', '(no title)')}",
            color = STATUS_COLORS.get(status, 0x2563EB),
        )
        embed.add_field(name="Status", value=status.capitalize(), inline=True)
        embed.add_field(name="Topic",  value=ticket.get("topic", "—").replace("_", " ").title(), inline=True)
        embed.set_footer(text="Trading Ranch Support · Reply in this thread to respond")
        return embed

    async def _invite_staff_to_thread(self, thread: discord.Thread):
        """Explicitly add every member with the staff role to a private thread."""
        staff_role_id = await self.config.staff_role_id()
        if not staff_role_id or not thread.guild:
            return
        role = thread.guild.get_role(staff_role_id)
        if not role:
            return
        for member in role.members:
            try:
                await thread.add_user(member)
            except discord.HTTPException:
                pass

    async def _archive_thread_for_ticket(self, ticket_id: int):
        """Archive and lock the Discord thread linked to a ticket."""
        threads = await self.config.ticket_threads()
        for tid_str, tid in threads.items():
            if tid == ticket_id:
                thread = self.bot.get_channel(int(tid_str))
                if thread and isinstance(thread, discord.Thread):
                    try:
                        await thread.edit(archived=True, locked=True)
                    except (discord.Forbidden, discord.HTTPException):
                        log.warning("Could not archive thread %s for ticket %s", tid_str, ticket_id)
                break

    async def _post_ticket_notification(
        self,
        ticket: dict,
        thread: discord.Thread,
        *,
        source_label: str = "🎫 Ticket",
        submitter: str = "",
        guild: discord.Guild = None,
    ):
        """Post a notification embed with Claim/Close/Resolved buttons to the
        configured notify channel.  Staff click Claim to join the thread."""
        notify_channel_id = await self.config.notify_channel_id()
        if not notify_channel_id:
            return
        g = guild or (thread.guild if thread else None)
        if not g:
            return
        channel = g.get_channel(notify_channel_id)
        if not channel:
            return

        ticket_id = ticket.get("id", "?")
        status    = ticket.get("status", "open")
        title     = ticket.get("title", "(no title)")

        embed = discord.Embed(
            title=f"{source_label} #{ticket_id} — {title}",
            color=STATUS_COLORS.get(status, 0x2563EB),
        )
        embed.add_field(name="Status", value=status.capitalize(), inline=True)
        embed.add_field(name="Topic",  value=ticket.get("topic", "—").replace("_", " ").title(), inline=True)
        if submitter:
            embed.add_field(name="From", value=submitter, inline=True)
        embed.add_field(name="Thread", value=f"<#{thread.id}>", inline=True)
        embed.set_footer(text="Click Claim to join this ticket's thread")

        # Ping the staff role so they notice the notification.
        staff_role_id = await self.config.staff_role_id()
        staff_ping = f"<@&{staff_role_id}>" if staff_role_id else ""

        try:
            await channel.send(
                content=staff_ping,
                embed=embed,
                view=TicketView(cog=self),
                allowed_mentions=discord.AllowedMentions(roles=True),
            )
        except (discord.Forbidden, discord.HTTPException) as exc:
            log.warning("Could not post ticket notification: %s", exc)

    async def _create_thread_for_web_ticket(
        self, ticket: dict, support_channel: discord.TextChannel
    ):
        """Create a private Discord thread for a web-originated ticket."""
        ticket_id = ticket.get("id")
        if not ticket_id:
            return

        # Skip if this ticket already has a Discord thread.
        if ticket.get("discord_thread_id"):
            return
        threads = await self.config.ticket_threads()
        for _, tid in threads.items():
            if tid == ticket_id:
                return

        # Extract submitter name from the ticket title ("Topic - Name").
        raw_title = ticket.get("title", "")
        submitter = raw_title.split(" - ", 1)[1] if " - " in raw_title else (ticket.get("discord_username", "") or "Web User")
        try:
            thread = await support_channel.create_thread(
                name=f"Ticket #{ticket_id} — {submitter}",
                type=discord.ChannelType.private_thread,
                auto_archive_duration=10080,
            )
        except (discord.Forbidden, discord.HTTPException) as exc:
            log.warning("Could not create thread for web ticket #%s: %s", ticket_id, exc)
            return

        # Persist the thread <-> ticket mapping.
        await self._register_thread(thread.id, ticket_id)
        await self._patch(f"/tickets/{ticket_id}", {"discord_thread_id": str(thread.id)})

        # Post ticket embed.
        embed = self._ticket_embed(ticket, prefix="\U0001f310 Web Ticket")
        if ticket.get("guest_email"):
            embed.add_field(name="Guest Email", value=ticket["guest_email"], inline=True)
        if ticket.get("wp_user_id"):
            wp_uid = ticket["wp_user_id"]
            # Try to get the WP display name via the linked Discord account.
            discord_uid = ticket.get("discord_user_id", "")
            wp_name = ""
            if discord_uid:
                wp_lookup = await self._wp_user_for(discord_uid)
                wp_name = wp_lookup.get("display_name", "")
            embed.add_field(
                name="Web Account", value=wp_name or f"User #{wp_uid}", inline=True,
            )
        embed_msg = await thread.send(embed=embed)

        # Post the ticket message content (fetched from the first reply).
        replies = await self._get(f"/tickets/{ticket_id}/replies")
        first_msg = ""
        if replies and isinstance(replies, list) and replies:
            first_msg = replies[0].get("message", "")
            # Seed the reply watermark so _sync_wp_replies doesn't re-post this.
            first_reply_id = replies[0].get("id", 0)
            if first_reply_id:
                async with self.config.last_reply_ids() as ids:
                    ids[str(ticket_id)] = first_reply_id
        if first_msg:
            await thread.send(f"**{submitter}** (via website):\n\n{first_msg}")

        # Post notification with Claim/Close/Resolved buttons.
        await self._post_ticket_notification(
            ticket, thread,
            source_label="\U0001f310 Web Ticket",
            submitter=submitter,
            guild=support_channel.guild,
        )

        # Topic selection via emoji reactions (staff or linked author can pick).
        topic_msg = await thread.send(self._topic_reaction_menu())
        for emoji in self._TOPIC_EMOJIS:
            try:
                await topic_msg.add_reaction(emoji)
            except discord.HTTPException:
                pass
        discord_user_id_raw = ticket.get("discord_user_id", "")
        self._topic_select[topic_msg.id] = {
            "ticket_id":    ticket_id,
            "thread_id":    thread.id,
            "author_id":    int(discord_user_id_raw) if discord_user_id_raw else 0,
            "embed_msg_id": embed_msg.id,
        }

        # If the web user has a linked Discord account, invite them too.
        discord_user_id = ticket.get("discord_user_id")
        if discord_user_id:
            try:
                member = support_channel.guild.get_member(int(discord_user_id))
                if member:
                    await thread.add_user(member)
                    await self._register_author(ticket_id, discord_user_id, True)
            except (ValueError, discord.HTTPException):
                pass

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
                await message.delete()
            except (discord.Forbidden, discord.HTTPException):
                pass
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

            # Resolve Discord mentions / channels and attach images.
            content = await self._clean_discord_text(content, message.guild)
            image_urls = self._collect_image_urls(message)
            if image_urls:
                content += "\n" + "\n".join(image_urls)

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
                wp_display = wp_info.get("display_name") or str(wp_user_id)
                embed.add_field(
                    name="Web Account", value=wp_display, inline=True,
                )
            embed_msg = await thread.send(embed=embed)
            await thread.send(f"**{author.display_name}:**\n\n{content}")

            # Welcome message — topic is fixed to "general".
            await thread.send(
                f"Thanks for reaching out, {author.mention}! A support team member "
                f"will be with you shortly."
            )

            # Post notification with Claim/Close/Resolved buttons.
            await self._post_ticket_notification(
                ticket, thread,
                source_label="🆕 New Ticket",
                submitter=f"{author.mention} ({author})",
                guild=message.guild,
            )

            # Topic selection via emoji reactions.
            topic_msg = await thread.send(self._topic_reaction_menu())
            for emoji in self._TOPIC_EMOJIS:
                try:
                    await topic_msg.add_reaction(emoji)
                except discord.HTTPException:
                    pass
            self._topic_select[topic_msg.id] = {
                "ticket_id":      ticket_id,
                "thread_id":      thread.id,
                "author_id":      author.id,
                "embed_msg_id":   embed_msg.id,
                "author_mention": author.mention,
                "author_str":     str(author),
                "wp_user_id":     wp_user_id,
            }

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

    # ── on_raw_reaction_add — topic selection ───────────────────────────────

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        """Handle emoji reactions for topic selection on new-ticket messages."""
        if payload.user_id == self.bot.user.id:
            return
        entry = self._topic_select.get(payload.message_id)
        if not entry:
            return
        # Allow the ticket author OR any staff member to select the topic.
        if payload.user_id != entry["author_id"]:
            guild = self.bot.get_guild(payload.guild_id) if payload.guild_id else None
            member = guild.get_member(payload.user_id) if guild else None
            if not member or not await self._is_staff(member):
                return
        emoji = str(payload.emoji)
        try:
            idx = self._TOPIC_EMOJIS.index(emoji)
        except ValueError:
            return
        slug, label = TOPICS[idx]
        result, code = await self._patch(f"/tickets/{entry['ticket_id']}", {"topic": slug})
        if code != 200:
            return
        # Consume the entry so double-reacts are ignored.
        del self._topic_select[payload.message_id]
        thread = self.bot.get_channel(entry["thread_id"])
        if not thread or not isinstance(thread, discord.Thread):
            return
        # Edit the react-menu message to confirm selection.
        try:
            topic_msg = await thread.fetch_message(payload.message_id)
            await topic_msg.edit(content=f"✅ Topic set to **{label}**.")
            await topic_msg.clear_reactions()
        except (discord.Forbidden, discord.HTTPException):
            pass
        # Update the ticket embed to reflect the new topic.
        try:
            embed_msg = await thread.fetch_message(entry["embed_msg_id"])
            old = embed_msg.embeds[0] if embed_msg.embeds else None
            if old:
                updated = discord.Embed(
                    title=old.title,
                    color=old.color,
                    description=old.description,
                )
                for field in old.fields:
                    val = label.replace("_", " ").title() if field.name == "Topic" else field.value
                    updated.add_field(name=field.name, value=val, inline=field.inline)
                if old.footer:
                    updated.set_footer(text=old.footer.text)
                await embed_msg.edit(embed=updated)
        except (discord.Forbidden, discord.HTTPException):
            pass

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

        # Resolve Discord mentions / channels to readable plain text.
        content = await self._clean_discord_text(content, message.guild)

        # Append image attachment URLs so they render on the web side.
        image_urls = self._collect_image_urls(message)
        if image_urls:
            content += "\n" + "\n".join(image_urls)

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

        if status_code not in (200, 201):
            try:
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
                await self._check_new_web_tickets()
            except asyncio.CancelledError:
                return
            except Exception:
                log.exception("Error in WP→Discord sync loop")
            await asyncio.sleep(SYNC_INTERVAL)

    async def _check_new_web_tickets(self):
        """Create Discord threads for new web-originated tickets.

        Notification is handled by the WordPress Discord webhook — this
        method only ensures every web ticket gets a staff-visible thread.
        """
        secret = await self.config.api_secret()
        if not secret:
            return
        channel_id = await self.config.channel_id()
        if not channel_id:
            return
        support_channel = self.bot.get_channel(channel_id)
        if not support_channel:
            return
        tickets = await self._get("/tickets")
        if not tickets or not isinstance(tickets, list):
            return
        last_id = await self.config.last_notified_ticket_id()

        # First run: seed the watermark to the current max ticket ID so we
        # only create threads for tickets submitted *after* setup.
        if last_id == 0:
            all_ids = [t.get("id", 0) for t in tickets]
            if all_ids:
                max_seed = max(all_ids)
                await self.config.last_notified_ticket_id.set(max_seed)
                log.info("Web-ticket watermark seeded to %s", max_seed)
            return

        # Only process web-origin tickets we haven't seen yet.
        new_tickets = [
            t for t in tickets
            if t.get("id", 0) > last_id and t.get("source", "web") != "discord"
        ]
        if not new_tickets:
            return
        new_tickets.sort(key=lambda t: t.get("id", 0))
        log.info("Creating threads for %d new web ticket(s)", len(new_tickets))
        for ticket in new_tickets:
            await self._create_thread_for_web_ticket(ticket, support_channel)
        max_id = max(t.get("id", 0) for t in new_tickets)
        if max_id > last_id:
            await self.config.last_notified_ticket_id.set(max_id)

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
                reply_text = f"🌐 **TradingRanchSupport** (bullbarbie.com):\n\n{msg_text}"

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

    @commands.group(name="trsupport", aliases=["trs"], invoke_without_command=True)
    @commands.guild_only()
    async def trsupport(self, ctx: commands.Context):
        """Trading Ranch Support Help Menu"""
        # Only send the menu when no subcommand was matched.
        # With invoke_without_command=True this callback always runs,
        # so we must guard against subcommand invocations.
        if ctx.invoked_subcommand is not None:
            return
        embed = discord.Embed(
            title="🎫 Trading Ranch Support Help Menu",
            color=0x1a0a2e,
        )
        embed.add_field(
            name="📋 Setup (Admin)",
            value=(
                "`[p]trs setchannel #channel` — where users submit tickets\n"
                "`[p]trs setnotifychannel #channel` — staff notification channel\n"
                "`[p]trs setstaffrole @role` — support staff role\n"
                "`[p]trs setsecret <key>` — WordPress API secret\n"
                "`[p]trs seturl <url>` — WordPress site URL\n"
                "`[p]trs settitle <text>` — change instructions embed title\n"
                "`[p]trs instructions` — post welcome embed\n"
                "`[p]trs settings` — view current config"
            ),
            inline=False,
        )
        embed.add_field(
            name="🛠️ Staff Commands",
            value=(
                "`[p]trs view [id]` — view ticket summary\n"
                "`[p]trs status [id] <status>` — update status\n"
                "`[p]trs close [id]` — close ticket & archive thread\n"
                "`[p]trs claim [id]` — assign ticket to you\n"
                "`[p]trs reply [id] <msg>` — reply to a ticket\n"
                "`[p]trs list [status]` — list tickets\n"
                "`[p]trs ping` — test WordPress connection"
            ),
            inline=False,
        )
        embed.add_field(
            name="💡 User Command",
            value="`[p]support` — directs users to the ticket channel",
            inline=False,
        )
        embed.set_footer(text="Run commands inside a ticket thread to skip the ticket ID.")
        await ctx.send(embed=embed)

    @trsupport.command(name="setchannel")
    @commands.admin_or_permissions(manage_guild=True)
    async def trs_setchannel(self, ctx: commands.Context, channel: discord.TextChannel):
        """Set the public channel where users type to open a support ticket.
        Any message sent there is deleted and a private thread is created automatically."""
        await self.config.channel_id.set(channel.id)
        await ctx.send(
            f"✅ Ticket submission channel set to {channel.mention}. "
            f"Users who type there will have their message removed and a private ticket thread created.\n"
            f"Run `[p]trsupport instructions` to post the welcome embed in that channel."
        )

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

    @trsupport.command(name="setnotifychannel")
    @commands.admin_or_permissions(manage_guild=True)
    async def trs_setnotifychannel(self, ctx: commands.Context, channel: discord.TextChannel):
        """Set the channel where ticket notifications with Claim/Close buttons appear.
        Staff click Claim to join a ticket thread. This should be a staff-only channel."""
        await self.config.notify_channel_id.set(channel.id)
        await ctx.send(
            f"✅ Notification channel set to {channel.mention}.\n"
            f"New tickets will post here with **Claim**, **Close**, and **Resolved** buttons."
        )

    # ── Default instructions text (source of truth) ──────────────────────────

    _DEFAULT_INSTR_TITLE       = "🎫 Trading Ranch Support"
    _DEFAULT_INSTR_DESCRIPTION = (
        "Need help? Click the **Open a Ticket** button below and describe "
        "your issue. A private support ticket thread will be created for you.\n\n"
        "How it works:\n"
        "1️⃣ Click the button and describe your issue\n"
        "2️⃣ A private ticket thread is created\n"
        "3️⃣ Chat directly with our staff privately\n\n"
        "Only you and the support team can see your thread. "
        "Include details and screenshots when applicable.\n\n"
        "Support tickets can also be opened on our website at "
        "[bullbarbie.com](https://bullbarbie.com), but please do not submit "
        "duplicate tickets as this slows us down."
    )
    _DEFAULT_INSTR_FOOTER      = "Trading Ranch Support System"
    _DEFAULT_INSTR_COLOR       = 0x1a0a2e

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
        title = await self.config.instr_title() or self._DEFAULT_INSTR_TITLE
        embed = discord.Embed(
            title=title,
            description=self._DEFAULT_INSTR_DESCRIPTION,
            color=self._DEFAULT_INSTR_COLOR,
        )
        embed.set_footer(text=self._DEFAULT_INSTR_FOOTER)
        await channel.send(embed=embed, view=TicketCreateView())
        if ctx.channel.id != channel_id:
            await ctx.send(f"✅ Instructions posted in {channel.mention}.")

    @trsupport.command(name="settitle")
    @commands.admin_or_permissions(manage_guild=True)
    async def trs_settitle(self, ctx: commands.Context, *, text: str):
        """Change the title of the instructions embed."""
        await self.config.instr_title.set(text)
        await ctx.send(f"✅ Instructions title set to: {text}\nRun `[p]trs instructions` to re-post.")

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
    async def trs_view(self, ctx: commands.Context, ticket_id: int = None):
        """View a ticket summary. Run inside a ticket thread to skip the ID."""
        ticket_id = await self._resolve_ticket_id(ctx, ticket_id)
        if ticket_id is None:
            return
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
    async def trs_setstatus(self, ctx: commands.Context, ticket_id_or_status: str, new_status: str = None):
        """Update a ticket's status. Run inside a thread: `$trs status open`.
        Outside a thread: `$trs status <id> <status>`."""
        # Allow `$trs status <status>` inside a thread (ticket_id_or_status is the status).
        if new_status is None:
            ticket_id = await self._resolve_ticket_id(ctx, None)
            if ticket_id is None:
                return
            new_status = ticket_id_or_status
        else:
            try:
                ticket_id = int(ticket_id_or_status)
            except ValueError:
                await ctx.send("Usage: `$trs status <ticket_id> <status>` or run inside a thread: `$trs status <status>`")
                return
        new_status = new_status.lower()
        if new_status not in STATUSES:
            await ctx.send(f"Invalid status. Choose from: `{'`, `'.join(STATUSES)}`")
            return
        result, code = await self._patch(f"/tickets/{ticket_id}", {"status": new_status})
        if code == 200:
            await ctx.send(f"✅ Ticket #{ticket_id} status updated to **{new_status}**.")
            if new_status in ("closed", "resolved"):
                await self._archive_thread_for_ticket(ticket_id)
        else:
            await ctx.send(f"❌ Failed to update ticket #{ticket_id}.")

    @trsupport.command(name="close")
    @commands.admin_or_permissions(manage_guild=True)
    async def trs_close(self, ctx: commands.Context, ticket_id: int = None):
        """Close a ticket and archive its Discord thread. (Staff only)"""
        ticket_id = await self._resolve_ticket_id(ctx, ticket_id)
        if ticket_id is None:
            return
        if not await self._is_staff(ctx.author):
            await ctx.send("❌ Only support staff can close tickets.")
            return
        result, code = await self._patch(f"/tickets/{ticket_id}", {"status": "closed"})
        if code == 200:
            await ctx.send(f"✅ Ticket #{ticket_id} has been closed.")
            await self._archive_thread_for_ticket(ticket_id)
        else:
            await ctx.send(f"❌ Could not close ticket #{ticket_id}.")

    @trsupport.command(name="claim")
    @commands.admin_or_permissions(manage_guild=True)
    async def trs_claim(self, ctx: commands.Context, ticket_id: int = None):
        """Claim a ticket. Run inside a ticket thread to skip the ID."""
        ticket_id = await self._resolve_ticket_id(ctx, ticket_id)
        if ticket_id is None:
            return
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
    async def trs_reply(self, ctx: commands.Context, ticket_id_or_msg: str, *, message: str = None):
        """Reply to a ticket. Run inside a thread: `$trs reply <message>`.
        Outside a thread: `$trs reply <id> <message>`."""
        # Allow `$trs reply <message>` inside a thread.
        if message is None:
            ticket_id = await self._resolve_ticket_id(ctx, None)
            if ticket_id is None:
                return
            message = ticket_id_or_msg
        else:
            try:
                ticket_id = int(ticket_id_or_msg)
            except ValueError:
                await ctx.send("Usage: `$trs reply <ticket_id> <message>` or run inside a thread: `$trs reply <message>`")
                return
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
        wp_url            = await self.config.wp_url()
        api_secret        = await self.config.api_secret()
        channel_id        = await self.config.channel_id()
        staff_role_id     = await self.config.staff_role_id()
        notify_channel_id = await self.config.notify_channel_id()

        channel        = ctx.guild.get_channel(channel_id) if channel_id and ctx.guild else None
        notify_channel = ctx.guild.get_channel(notify_channel_id) if notify_channel_id and ctx.guild else None
        staff_role     = ctx.guild.get_role(staff_role_id) if staff_role_id and ctx.guild else None
        threads        = await self.config.ticket_threads()

        embed = discord.Embed(title="BB TR Support — Settings", color=0x1a0a2e)
        embed.add_field(name="WordPress URL",      value=f"`{wp_url}`",                                            inline=False)
        embed.add_field(name="API Secret",         value="✅ Set" if api_secret else "❌ Not set",                 inline=True)
        embed.add_field(name="Ticket Channel",     value=channel.mention if channel else "❌ Not set",             inline=True)
        embed.add_field(name="Notify Channel",     value=notify_channel.mention if notify_channel else "❌ Not set", inline=True)
        embed.add_field(name="Staff Role",         value=staff_role.mention if staff_role else "❌ Not set",       inline=True)
        embed.add_field(name="Active Threads",     value=str(len(threads)),                                        inline=True)
        embed.add_field(name="WP Sync",            value=f"Every {SYNC_INTERVAL}s",                                inline=True)
        await ctx.send(embed=embed)
