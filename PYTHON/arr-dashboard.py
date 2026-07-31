#!/usr/bin/env python3
# Entry point for the arr-queue-cleaner auto-fix job's dashboard. The actual
# HTTP server and data-fetching logic live in arr_dashboard/; HTML/CSS/JS
# live in the HTML/, CSS/, and JS/ folders alongside this script's repo root.
from arr_dashboard.server import main

if __name__ == "__main__":
    main()
