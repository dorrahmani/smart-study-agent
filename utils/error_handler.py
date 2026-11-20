"""
Error Handler - smart error handling with retry logic
"""

import time
import functools
from typing import Callable, Any, Optional
import logging

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
for _noisy in ('httpx', 'httpcore'):                  # at INFO they log each URL (URLs can carry keys)
    logging.getLogger(_noisy).setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


def retry_on_failure(max_attempts: int = 3, delay: int = 2, backoff: int = 2):
    """
    Decorator that retries on error

    Args:
        max_attempts: maximum number of attempts
        delay: wait in seconds between attempts
        backoff: wait multiplier (exponential backoff)
"""
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args, **kwargs) -> Any:
            current_delay = delay
            
            for attempt in range(1, max_attempts + 1):
                try:
                    return func(*args, **kwargs)
                
                except Exception as e:
                    if attempt == max_attempts:
                        logger.error(f"❌ {func.__name__} נכשל אחרי {max_attempts} ניסיונות: {e}")
                        raise
                    
                    logger.warning(f"⚠️  {func.__name__} נכשל (ניסיון {attempt}/{max_attempts}): {e}")
                    logger.info(f"⏳ ממתין {current_delay} שניות לפני ניסיון נוסף...")
                    
                    time.sleep(current_delay)
                    current_delay *= backoff
            
        return wrapper
    return decorator


class SafeExecutor:
    """Runs functions safely with logging"""
    
    @staticmethod
    def execute(
        func: Callable,
        *args,
        task_name: str = "Task",
        critical: bool = False,
        **kwargs
    ) -> Optional[Any]:
        """
        Runs a function safely

        Args:
            func: the function to run
            task_name: task name for logging
            critical: whether this is a critical task (raise the exception)

        Returns:
            the function's result, or None on error
"""
        try:
            logger.info(f"▶️  מתחיל: {task_name}")
            result = func(*args, **kwargs)
            logger.info(f"✅ הושלם: {task_name}")
            return result
        
        except Exception as e:
            logger.error(f"❌ נכשל: {task_name} - {e}")
            
            if critical:
                raise
            
            return None


# Usage examples:

@retry_on_failure(max_attempts=3, delay=2)
def download_with_retry(url: str):
    """Example - download with retry"""
    # the download code goes here
    pass


# or without a decorator:
def some_risky_function():
    executor = SafeExecutor()
    
    result = executor.execute(
        download_file,
        url="http://example.com/file.pdf",
        task_name="הורדת קובץ PDF",
        critical=False  # don't stop if it fails
    )
    
    if result:
        print("הצלחה!")
    else:
        print("נכשל, ממשיכים...")
