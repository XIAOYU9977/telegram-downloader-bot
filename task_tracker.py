import asyncio
import logging
from typing import Dict, List, Set, Union, Any

logger = logging.getLogger(__name__)

class TaskTracker:
    """
    Tracks and manages active subprocesses and asyncio tasks per user.
    Enables robust cancellation of all ongoing operations.
    """
    def __init__(self):
        # user_id -> set of subprocess process
        self._processes: Dict[int, Set[Any]] = {}
        # user_id -> set of asyncio.Task
        self._tasks: Dict[int, Set[asyncio.Task]] = {}

    def register_process(self, user_id: int, process: Any):
        """Track a running subprocess for a user"""
        if user_id not in self._processes:
            self._processes[user_id] = set()
        self._processes[user_id].add(process)
        logger.debug(f"Registered process {process.pid} for user {user_id}")

    def unregister_process(self, user_id: int, process: Any):
        """Stop tracking a subprocess (usually after it finishes)"""
        if user_id in self._processes:
            self._processes[user_id].discard(process)
            if not self._processes[user_id]:
                del self._processes[user_id]
        logger.debug(f"Unregistered process for user {user_id}")

    def register_task(self, user_id: int, task: asyncio.Task):
        """Track an asyncio task for a user"""
        if user_id not in self._tasks:
            self._tasks[user_id] = set()
        self._tasks[user_id].add(task)
        logger.debug(f"Registered task for user {user_id}")

    def unregister_task(self, user_id: int, task: asyncio.Task):
        """Stop tracking an asyncio task"""
        if user_id in self._tasks:
            self._tasks[user_id].discard(task)
            if not self._tasks[user_id]:
                del self._tasks[user_id]
        logger.debug(f"Unregistered task for user {user_id}")

    async def cancel_all(self, user_id: int):
        """
        Forcefully terminate all processes and cancel all tasks for a user.
        """
        logger.info(f"🛑 Cancelling all operations for user {user_id}")

        # 1. Terminate subprocesses
        if user_id in self._processes:
            procs = self._processes[user_id].copy()
            for proc in procs:
                try:
                    if proc.returncode is None:
                        logger.info(f"Terminating process {proc.pid} for user {user_id}")
                        proc.terminate()
                        # Give it a moment to terminate gracefully, then kill if needed
                        try:
                            await asyncio.wait_for(proc.wait(), timeout=2.0)
                        except asyncio.TimeoutError:
                            logger.warning(f"Process {proc.pid} did not terminate, killing...")
                            proc.kill()
                except Exception as e:
                    logger.error(f"Error terminating process: {e}")
            del self._processes[user_id]

        # 2. Cancel asyncio tasks
        if user_id in self._tasks:
            tasks = self._tasks[user_id].copy()
            for task in tasks:
                if not task.done():
                    logger.info(f"Cancelling task for user {user_id}")
                    task.cancel()
            
            # Wait for tasks to be cancelled
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            del self._tasks[user_id]
            
        logger.info(f"✅ All operations cancelled for user {user_id}")

# Singleton instance
task_tracker = TaskTracker()
