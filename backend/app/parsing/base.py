"""解析层公共契约：提取结果的数据结构与 Extractor 协议。

解析器只负责"把文档变成页 + 条款树"，不碰业务。所有提取结果必须可溯源：
每个节点带页码与字符区间，每个页带宽高（证据 bbox 的归一化分母）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class ExtractedPage:
    page_no: int  # 从 1 开始
    text: str
    width: float  # points
    height: float  # points
    has_text_layer: bool = True
    page_approximate: bool = False  # docx 虚拟分页
    source: str = "text"  # text|ocr|skipped
    # 逐行 bbox（与 text.split("\n") 一一对应；PDF 提供，docx 为 None）。
    # 用途：表格区域排除编号扫描、证据定位的行级粗筛。
    line_boxes: list[tuple[float, float, float, float]] | None = None


@dataclass
class ExtractedNode:
    """条款树节点。node_id 由 chapter_tree 统一分配（路径编码）。"""

    node_id: str
    parent_node_id: str | None
    kind: str  # document|chapter|section|article|item|table|table_row
    number: str | None = None  # 原样编号文本："第三章"、"（一）"、"2.3.1"
    label: str | None = None  # 标准化标签："C3"、"I(一)"、"N2.3.1"
    title: str | None = None
    page: int = 1
    char_start: int = 0
    char_end: int = 0
    text: str = ""
    is_toc: bool = False
    is_coarse: bool = False
    depth: int = 0


@dataclass
class ExtractedDocument:
    pages: list[ExtractedPage]
    nodes: list[ExtractedNode] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    engine: str = "unknown"
    # 文档作者元数据（docx core_properties / pdf metadata）——串通检测信号源
    author: str | None = None
    # {page_no: [bbox, ...]} —— 表格区域（供条款树排除编号扫描、供评分表结构化）
    table_boxes: dict[int, list[tuple[float, float, float, float]]] = field(default_factory=dict)
    # 有内容损失迹象（单页提取失败、表格探测失败等）→ 落库为 degraded，不静默
    degraded: bool = False


class DocumentRejectedError(Exception):
    """解析前的拒绝（稳定机器码见 config.REJECT_*）。"""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


class Extractor(Protocol):
    """解析器协议：输入文件路径，输出页与条款树。

    实现者只允许抛 DocumentRejectedError（可预期拒绝）或让意外异常冒泡
    （由任务层捕获并落库为 failed）。
    """

    name: str

    def supports(self, filename: str) -> bool: ...

    def extract(self, path: str) -> ExtractedDocument: ...
