import logging
from logging.handlers import RotatingFileHandler
import os
import sys

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LOG_FILE_PATH = os.path.join(ROOT_DIR, "application.log")


def setup_logging():
    formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG) 
    
    
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO) 
    console_handler.setFormatter(formatter)
    
    root_logger.addHandler(console_handler)

    try:
        log_dir = os.path.dirname(LOG_FILE_PATH)
        if not os.path.exists(log_dir):
            os.makedirs(log_dir)

        file_handler = RotatingFileHandler(
            LOG_FILE_PATH,
            encoding='utf-8'
        )
        file_handler.setLevel(logging.DEBUG) 
        file_handler.setFormatter(formatter)

        root_logger.addHandler(file_handler)
        
        print(f"Logging configured successfully...")

    except Exception as e:
        print(f"FATAL ERROR setting up file logging: {e}. Continue without file output.")