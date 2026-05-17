import sys

if "--version" in sys.argv[1:]:
    from lilical import __version__
    print(f"lilical {__version__}")
    raise SystemExit(0)

from lilical.app import main

raise SystemExit(main())
