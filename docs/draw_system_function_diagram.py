from pathlib import Path
from xml.sax.saxutils import escape


CANVAS_WIDTH = 2200
CANVAS_HEIGHT = 1380

ROOT_LABEL = "癫痫腕表数据标注平台"

MODULES = [
    (
        "用户与项目管理模块",
        [
            "用户注册/登录",
            "创建项目",
            "编辑项目",
            "删除项目",
            "项目查看",
        ],
    ),
    (
        "资料导入与时间提取模块",
        [
            "录入患者基本信息",
            "导入视频文件",
            "导入腕表数据文件",
            "设置采样率",
            "保存项目资料",
            "提取视频开始/结束时间",
            "提取脑电标记开始/结束时间",
            "提取结果回写项目",
        ],
    ),
    (
        "对齐与信号浏览模块",
        [
            "播放/暂停/拖动视频",
            "浏览腕表波形与能量轴",
            "选择模态显示并设置滤波",
            "自动估计视频与腕表偏移",
            "手动拖拽微调偏移",
        ],
    ),
    (
        "标注作业模块",
        [
            "新增/修改/删除标注类型",
            "标记开始/结束时间",
            "拖拽波形直接框选区域",
            "保存标注",
            "删除标注",
            "查看当前标注列表",
        ],
    ),
    (
        "标注记录模块",
        [
            "查看项目标注记录",
            "显示标注开始/结束时间",
            "显示标注持续时间",
            "按项目查看历史标注",
        ],
    ),
]


def multiline_text_lines(text, max_chars):
    raw_lines = []
    for part in str(text).split("\n"):
        current = part.strip()
        while len(current) > max_chars:
            raw_lines.append(current[:max_chars])
            current = current[max_chars:]
        raw_lines.append(current)
    return [line for line in raw_lines if line]


def draw_text(center_x, center_y, text, font_size, bold=True, max_chars=10):
    lines = multiline_text_lines(text, max_chars=max_chars)
    if not lines:
        lines = [""]
    weight = "700" if bold else "400"
    line_height = font_size + 6
    start_y = center_y - (len(lines) - 1) * line_height / 2
    tspans = []
    for index, line in enumerate(lines):
        dy = 0 if index == 0 else line_height
        tspans.append(
            f'<tspan x="{center_x}" dy="{dy if index > 0 else 0}">{escape(line)}</tspan>'
        )
    first_line_y = start_y
    return (
        f'<text x="{center_x}" y="{first_line_y}" text-anchor="middle" '
        f'font-size="{font_size}" font-family="Microsoft YaHei, SimHei, sans-serif" '
        f'font-weight="{weight}" fill="#000000">'
        + "".join(tspans)
        + "</text>"
    )


def draw_box(center_x, center_y, width, height, text, facecolor, edgecolor, font_size=26, stroke_width=3, max_chars=10):
    left = center_x - width / 2
    top = center_y - height / 2
    rect = (
        f'<rect x="{left}" y="{top}" width="{width}" height="{height}" rx="26" ry="26" '
        f'fill="{facecolor}" stroke="{edgecolor}" stroke-width="{stroke_width}" />'
    )
    text_svg = draw_text(center_x, center_y + 2, text, font_size, bold=True, max_chars=max_chars)
    return rect + text_svg, {
        "left": left,
        "right": left + width,
        "top": top,
        "bottom": top + height,
        "center_x": center_x,
        "center_y": center_y,
    }


def draw_arrow(start_x, start_y, end_x, end_y, color="#6D9EEB", stroke_width=3):
    angle = 0.0
    dx = end_x - start_x
    dy = end_y - start_y
    if dx == 0 and dy == 0:
        angle = 0.0
    else:
        import math

        angle = math.atan2(dy, dx)
    arrow_len = 16
    wing = 7
    import math

    x1 = end_x - arrow_len * math.cos(angle) + wing * math.sin(angle)
    y1 = end_y - arrow_len * math.sin(angle) - wing * math.cos(angle)
    x2 = end_x - arrow_len * math.cos(angle) - wing * math.sin(angle)
    y2 = end_y - arrow_len * math.sin(angle) + wing * math.cos(angle)
    return (
        f'<line x1="{start_x}" y1="{start_y}" x2="{end_x}" y2="{end_y}" '
        f'stroke="{color}" stroke-width="{stroke_width}" />'
        f'<polygon points="{end_x},{end_y} {x1},{y1} {x2},{y2}" fill="{color}" />'
    )


def main():
    figure_dir = Path(__file__).resolve().parent
    output_path = figure_dir / "system_function_module_diagram.svg"

    svg_parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{CANVAS_WIDTH}" height="{CANVAS_HEIGHT}" viewBox="0 0 {CANVAS_WIDTH} {CANVAS_HEIGHT}">',
        '<rect width="100%" height="100%" fill="#FFFFFF" />',
    ]

    root_svg, root_box = draw_box(
        center_x=CANVAS_WIDTH / 2,
        center_y=90,
        width=520,
        height=76,
        text=ROOT_LABEL,
        facecolor="#A9D18E",
        edgecolor="#70AD47",
        font_size=30,
        stroke_width=4,
        max_chars=14,
    )
    svg_parts.append(root_svg)

    module_x_positions = [220, 660, 1100, 1540, 1980]
    module_y = 250
    child_top = 400
    module_width = 280
    module_height = 78
    child_width = 260
    child_height = 66
    child_spacing = 95

    for (module_name, functions), module_x in zip(MODULES, module_x_positions):
        module_svg, module_box = draw_box(
            center_x=module_x,
            center_y=module_y,
            width=module_width,
            height=module_height,
            text=module_name,
            facecolor="#C6E0B4",
            edgecolor="#70AD47",
            font_size=24,
            stroke_width=3,
            max_chars=9,
        )
        svg_parts.append(draw_arrow(root_box["center_x"], root_box["bottom"], module_box["center_x"], module_box["top"]))
        svg_parts.append(module_svg)

        for index, child_name in enumerate(functions):
            child_y = child_top + index * child_spacing
            child_svg, child_box = draw_box(
                center_x=module_x,
                center_y=child_y,
                width=child_width,
                height=child_height,
                text=child_name,
                facecolor="#F8CBAD",
                edgecolor="#ED7D31",
                font_size=19,
                stroke_width=2,
                max_chars=10,
            )
            svg_parts.append(
                draw_arrow(
                    module_box["center_x"],
                    module_box["bottom"],
                    child_box["center_x"],
                    child_box["top"],
                    color="#7EA6E0",
                    stroke_width=2,
                )
            )
            svg_parts.append(child_svg)

    svg_parts.append(
        draw_text(
            CANVAS_WIDTH / 2,
            CANVAS_HEIGHT - 40,
            "图  系统总体功能模块设计图",
            font_size=28,
            bold=True,
            max_chars=20,
        )
    )
    svg_parts.append("</svg>")

    output_path.write_text("".join(svg_parts), encoding="utf-8")
    print(f"已生成图片：{output_path}")


if __name__ == "__main__":
    main()
