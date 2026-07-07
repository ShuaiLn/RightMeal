"""RightMeal entry point."""

import flet as ft
from dotenv import load_dotenv

from ui.app import main

load_dotenv()

if __name__ == "__main__":
    ft.run(main)
