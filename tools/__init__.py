from .pdf2word import router as pdf2word_router
from .word2pdf import router as word2pdf_router

TOOL_REGISTRY = [
    {
        "name": "PDF 转 Word",
        "slug": "pdf2word",
        "description": "纯文本 / 表格 PDF 转 Word：合并单元格、图片嵌入、页码范围、批量 ZIP。",
        "icon": "📄",
        "route": "/tools/pdf2word",
    },
    {
        "name": "Word 转 PDF",
        "slug": "word2pdf",
        "description": "Word（.docx / .doc）转 PDF：LibreOffice 优先，Windows 可回退 Microsoft Word。",
        "icon": "📝",
        "route": "/tools/word2pdf",
    },
]

__all__ = ["TOOL_REGISTRY", "pdf2word_router", "word2pdf_router"]
