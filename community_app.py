"""Entry point for the hosted Streamlit Community Cloud app."""

import os

from app import main


if __name__ == "__main__":
    os.environ["APP_MODE"] = "cloud"
    os.environ["SMART_HOME_COMMUNITY_CLOUD"] = "1"
    main()
