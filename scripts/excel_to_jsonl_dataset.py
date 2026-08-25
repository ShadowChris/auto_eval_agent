"""将 XLSX/CSV 表格转换为任务类 JSONL 数据集的命令行入口。"""

from auto_eval.table_dataset import (
    ConversionResult,
    build_parser,
    convert_table,
    main,
)

__all__ = ["ConversionResult", "build_parser", "convert_table", "main"]


if __name__ == "__main__":
    raise SystemExit(main())
