# -*- coding: utf-8 -*-
"""kikoeru 字幕管线入口。

    python pipeline/pipeline.py run --project RJ9000010 --source $KIKOERU_ROOT/RJ9000010
    python pipeline/pipeline.py status
    python pipeline/pipeline.py check --run <run_id>
    python pipeline/pipeline.py repair --run <run_id> --plan repairs.json --apply
    python pipeline/pipeline.py explain --run <run_id> --track "09.xxx.mp3" --start 346.5
    python pipeline/pipeline.py knowledge status
    python pipeline/pipeline.py selftest
    python pipeline/pipeline.py version

约定：解释器用装了依赖的那个 venv（见 pipeline.json / pipeline_lib/paths.py）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pipeline_lib.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
