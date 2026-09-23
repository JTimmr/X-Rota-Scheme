import io
import os
import unicodedata
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import discord
from PIL import Image, ImageDraw, ImageFont

from config import ROTA_TIMEZONE
from role_rota_database import (
    ROLES,
    STATUS_ACTIVE,
    STATUS_BACKUP,
    Assignment,
)


CRITICAL_ROLES = {
    "Main Scheduler",
    "Sniping & Raid Replies",
    "Live Response",
}

CANVAS_BG = (244, 246, 248)
CARD_BG = (255, 255, 255)
HEADER_BG = (25, 27, 30)
TEXT = (31, 35, 40)
MUTED = (112, 120, 131)
GRID = (218, 223, 229)
ROW_ALT = (250, 251, 252)
ACTIVE_BG = (181, 244, 192)
ACTIVE_TEXT = (20, 92, 39)
BACKUP_BG = (255, 222, 112)
BACKUP_TEXT = (112, 71, 0)
WARNING_BG = (255, 226, 226)
WARNING_TEXT = (176, 33, 33)


def clean_display_name(name: str) -> str:
    """Strip glyphs that commonly render as empty boxes in the rota image."""
    chars: list[str] = []
    for character in name:
        category = unicodedata.category(character)
        if category.startswith("C") or category in {"So", "Sk"}:
            continue
        chars.append(character)
    cleaned = " ".join("".join(chars).split()).strip()
    return cleaned or "Discord User"


def _font(
    size: int,
    bold: bool = False,
) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates: list[str] = []
    if os.name == "nt":
        candidates.extend(
            [
                (
                    r"C:\Windows\Fonts\seguisb.ttf"
                    if bold
                    else r"C:\Windows\Fonts\segoeui.ttf"
                ),
                (
                    r"C:\Windows\Fonts\arialbd.ttf"
                    if bold
                    else r"C:\Windows\Fonts\arial.ttf"
                ),
            ]
        )
    candidates.extend(
        [
            (
                "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
                if bold
                else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
            ),
            (
                "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf"
                if bold
                else "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf"
            ),
        ]
    )
    for path in candidates:
        if os.path.exists(path):
            return ImageFont.truetype(path, size=size)
    return ImageFont.load_default()


def _fit_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    font,
    max_width: int,
) -> str:
    if draw.textbbox((0, 0), text, font=font)[2] <= max_width:
        return text

    suffix = "…"
    output = text
    while (
        output
        and draw.textbbox((0, 0), output + suffix, font=font)[2] > max_width
    ):
        output = output[:-1]
    return (output + suffix) if output else suffix


def _centered_text(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    text: str,
    font,
    fill,
) -> None:
    x0, y0, x1, y1 = box
    bounds = draw.textbbox((0, 0), text, font=font)
    width = bounds[2] - bounds[0]
    height = bounds[3] - bounds[1]
    draw.text(
        ((x0 + x1 - width) / 2, (y0 + y1 - height) / 2 - 2),
        text,
        font=font,
        fill=fill,
    )


def _timestamp() -> str:
    try:
        zone = ZoneInfo(ROTA_TIMEZONE)
    except Exception:
        zone = timezone.utc
    return datetime.now(zone).strftime("Updated %d %b %Y · %H:%M")


def render_rota_image(
    members: list[tuple[int, str, bool]],
    assignments: list[Assignment],
) -> io.BytesIO:
    width = 1450
    outer = 28
    card_pad = 24
    title_h = 94
    stats_h = 66
    header_h = 78
    row_h = 88
    footer_h = 54
    rows_to_draw = max(1, len(members))
    height = (
        outer * 2
        + title_h
        + stats_h
        + header_h
        + rows_to_draw * row_h
        + footer_h
    )

    image = Image.new("RGB", (width, height), CANVAS_BG)
    draw = ImageDraw.Draw(image)

    title_font = _font(46, bold=True)
    stat_font = _font(16, bold=True)
    header_font = _font(20, bold=True)
    name_font = _font(27, bold=True)
    status_font = _font(24, bold=True)
    footer_font = _font(17)

    left, top = outer, outer
    right, bottom = width - outer, height - outer
    draw.rounded_rectangle(
        (left, top, right, bottom),
        radius=20,
        fill=CARD_BG,
        outline=GRID,
        width=2,
    )

    title = "CTO X TEAM — LIVE ROTA"
    title_bounds = draw.textbbox((0, 0), title, font=title_font)
    title_width = title_bounds[2] - title_bounds[0]
    draw.text(
        ((width - title_width) / 2, top + 20),
        title,
        font=title_font,
        fill=TEXT,
    )

    table_left = left + card_pad
    table_right = right - card_pad
    table_top = top + title_h + stats_h
    table_width = table_right - table_left

    column_widths = [240, 195, 205, 250, 170]
    column_widths.append(table_width - sum(column_widths))
    x_positions = [table_left]
    for column_width in column_widths:
        x_positions.append(x_positions[-1] + column_width)

    assignments_by_user: dict[int, dict[str, str]] = {}
    for assignment in assignments:
        assignments_by_user.setdefault(assignment.user_id, {})[
            assignment.role_name
        ] = assignment.status

    active_by_role = {role: 0 for role in ROLES}
    backup_by_role = {role: 0 for role in ROLES}
    for assignment in assignments:
        if assignment.role_name not in active_by_role:
            continue
        if assignment.status == STATUS_ACTIVE:
            active_by_role[assignment.role_name] += 1
        elif assignment.status == STATUS_BACKUP:
            backup_by_role[assignment.role_name] += 1

    stats_top = top + title_h
    stats_bottom = stats_top + stats_h
    for index in range(6):
        x0, x1 = x_positions[index], x_positions[index + 1]
        if index == 0:
            count = len(members)
            stats_text = f"{count} MEMBER{'S' if count != 1 else ''}"
            fill = MUTED
            background = CARD_BG
        else:
            role = ROLES[index - 1]
            active = active_by_role[role]
            backup = backup_by_role[role]
            stats_text = f"{active} ACTIVE\n{backup} BACKUP"
            critical_shortage = role in CRITICAL_ROLES and active == 0
            fill = WARNING_TEXT if critical_shortage else MUTED
            background = WARNING_BG if critical_shortage else CARD_BG

        draw.rectangle((x0, stats_top, x1, stats_bottom), fill=background)
        if "\n" in stats_text:
            bounds = draw.multiline_textbbox(
                (0, 0),
                stats_text,
                font=stat_font,
                spacing=1,
                align="center",
            )
            text_width = bounds[2] - bounds[0]
            text_height = bounds[3] - bounds[1]
            draw.multiline_text(
                (
                    (x0 + x1 - text_width) / 2,
                    stats_top + (stats_h - text_height) / 2 - 1,
                ),
                stats_text,
                font=stat_font,
                fill=fill,
                spacing=1,
                align="center",
            )
        else:
            _centered_text(
                draw,
                (x0, stats_top, x1, stats_bottom),
                stats_text,
                stat_font,
                fill,
            )

    draw.rectangle(
        (table_left, table_top, table_right, table_top + header_h),
        fill=HEADER_BG,
    )
    headers = [
        "USER",
        "MAIN\nSCHEDULER",
        "CONTENT\nCURATORS",
        "SNIPING & RAID\nREPLIES",
        "RESEARCH",
        "LIVE\nRESPONSE",
    ]
    for index, header in enumerate(headers):
        x0, x1 = x_positions[index], x_positions[index + 1]
        bounds = draw.multiline_textbbox(
            (0, 0),
            header,
            font=header_font,
            spacing=2,
            align="center",
        )
        text_width = bounds[2] - bounds[0]
        text_height = bounds[3] - bounds[1]
        draw.multiline_text(
            (
                (x0 + x1 - text_width) / 2,
                table_top + (header_h - text_height) / 2 - 2,
            ),
            header,
            font=header_font,
            fill=CARD_BG,
            spacing=2,
            align="center",
        )

    rows = members if members else [(0, "No team members yet", False)]
    y = table_top + header_h
    for row_index, (user_id, display_name, _) in enumerate(rows):
        row_bottom = y + row_h
        row_fill = CARD_BG if row_index % 2 == 0 else ROW_ALT
        draw.rectangle(
            (table_left, y, table_right, row_bottom),
            fill=row_fill,
        )

        name = _fit_text(
            draw,
            clean_display_name(display_name),
            name_font,
            column_widths[0] - 30,
        )
        name_bounds = draw.textbbox((0, 0), name, font=name_font)
        name_height = name_bounds[3] - name_bounds[1]
        draw.text(
            (table_left + 16, y + (row_h - name_height) / 2 - 2),
            name,
            font=name_font,
            fill=TEXT,
        )

        role_map = assignments_by_user.get(user_id, {})
        for role_index, role in enumerate(ROLES, start=1):
            x0, x1 = x_positions[role_index], x_positions[role_index + 1]
            status = role_map.get(role)
            if status == STATUS_ACTIVE:
                draw.rectangle(
                    (x0 + 2, y + 2, x1 - 2, row_bottom - 2),
                    fill=ACTIVE_BG,
                )
                _centered_text(
                    draw,
                    (x0, y, x1, row_bottom),
                    "ACTIVE",
                    status_font,
                    ACTIVE_TEXT,
                )
            elif status == STATUS_BACKUP:
                draw.rectangle(
                    (x0 + 2, y + 2, x1 - 2, row_bottom - 2),
                    fill=BACKUP_BG,
                )
                _centered_text(
                    draw,
                    (x0, y, x1, row_bottom),
                    "BACKUP",
                    status_font,
                    BACKUP_TEXT,
                )
            else:
                _centered_text(
                    draw,
                    (x0, y, x1, row_bottom),
                    "—",
                    status_font,
                    MUTED,
                )

        draw.line(
            (table_left, row_bottom, table_right, row_bottom),
            fill=GRID,
            width=2,
        )
        y = row_bottom

    for x in x_positions[1:-1]:
        draw.line((x, stats_top, x, y), fill=GRID, width=2)

    stamp = _timestamp()
    stamp_bounds = draw.textbbox((0, 0), stamp, font=footer_font)
    stamp_width = stamp_bounds[2] - stamp_bounds[0]
    draw.text(
        (table_right - stamp_width, y + 17),
        stamp,
        font=footer_font,
        fill=MUTED,
    )

    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    output.seek(0)
    return output


def rota_file(
    members: list[tuple[int, str, bool]],
    assignments: list[Assignment],
) -> discord.File:
    return discord.File(
        render_rota_image(members, assignments),
        filename="cto_x_live_rota.png",
    )
