#!/usr/bin/env python3
"""Run from a repository checkout or a self-contained installed Skill."""

import sys
from pathlib import Path

here = Path(__file__).resolve()
package_root = here.parent if (here.parent / "oceanbase_compete").is_dir() else here.parents[3] / "src"
sys.path.insert(0, str(package_root))

from oceanbase_compete.cli import main

raise SystemExit(main())
