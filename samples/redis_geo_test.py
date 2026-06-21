#!/usr/bin/env python3
"""
Redis GEO Queue Test Script
Test connection to Linode Redis and demonstrate push/pull operations
"""

import redis
import json
import hashlib
import time
import uuid
from datetime import datetime

# Redis connection settings (Linode server)
REDIS_HOST = "172.235.215.18"  # Updated after Terraform apply
REDIS_PORT = 6379
REDIS_PASSWORD = "GEO_SYSTEM_STRONG_SECRET_AUTH_KEY"
REDIS_DB = 0

# Queue name
QUEUE_NAME = "Queue:GEO:Tasks"
FAILED_QUEUE_NAME = "Queue:GEO:Failed"
DEAD_LETTER_QUEUE_NAME = "Queue:GEO:DeadLetter"
LOCK_PREFIX = "Lock:GEO:Analysis"

class RedisGEOClient:
    def __init__(self, host, port, password, db=0):
        self.r = redis.Redis(
            host=host,
            port=port,
            password=password,
            db=db,
            decode_responses=True
        )
        self._check_connection()
    
    def _check_connection(self):
        """Check Redis connection"""
        try:
            self.r.ping()
            print("✅ Redis connected successfully!")
        except redis.ConnectionError as e:
            print(f"❌ Redis connection failed: {e}")
            raise
    
    def _get_url_hash(self, url):
        """Generate SHA256 hash for URL"""
        return hashlib.sha256(url.encode()).hexdigest()
    
    def _get_lock_key(self, url):
        """Generate distributed lock key"""
        url_hash = self._get_url_hash(url)
        return f"{LOCK_PREFIX}:{url_hash}"
    
    def acquire_lock(self, url, worker_token, ttl=3600):
        """
        Acquire distributed lock for URL
        Returns: True if lock acquired, False otherwise
        """
        lock_key = self._get_lock_key(url)
        # SET NX (Not eXists) with TTL
        acquired = self.r.set(lock_key, worker_token, nx=True, ex=ttl)
        if acquired:
            print(f"🔒 Lock acquired for {url[:50]}...")
        else:
            print(f"⏭️ Lock already exists for {url[:50]}... skipping")
        return acquired
    
    def release_lock(self, url, worker_token):
        """
        Release distributed lock safely
        Only releases if token matches (prevents race conditions)
        """
        lock_key = self._get_lock_key(url)
        
        # Lua script for atomic check-and-delete
        lua_script = """
        if redis.call("get", KEYS[1]) == ARGV[1] then
            return redis.call("del", KEYS[1])
        else
            return 0
        end
        """
        
        result = self.r.eval(lua_script, 1, lock_key, worker_token)
        if result:
            print(f"🔓 Lock released for {url[:50]}...")
        return bool(result)
    
    def push_to_queue(self, url, priority=0):
        """
        Push URL to analysis queue with distributed lock check
        
        Args:
            url: URL to analyze
            priority: Higher number = higher priority
        
        Returns:
            bool: True if pushed successfully, False if already processing
        """
        request_id = uuid.uuid4().hex
        token = uuid.uuid4().hex
        
        # Try to acquire lock first
        if not self.acquire_lock(url, token, ttl=3600):
            print(f"⚠️ URL already in queue or being processed: {url[:50]}...")
            return False
        
        # Create task data
        task = {
            "request_id": request_id,
            "url": url,
            "mode": "full",
            "priority": priority,
            "token": token,
            "enqueued_at": datetime.now().isoformat(),
            "attempts": 0
        }
        
        # Push to queue (using Redis list as queue)
        # Using LPUSH for priority queue simulation
        try:
            self.r.lpush(QUEUE_NAME, json.dumps(task))
            print(f"✅ Task pushed to queue: {url[:50]}... (priority: {priority})")
        except Exception:
            self.release_lock(url, token)
            raise
        
        return True
    
    def pull_from_queue(self, worker_token, timeout=30):
        """
        Pull task from queue (blocking pop)
        
        Args:
            worker_token: Unique token for this worker
            timeout: Block timeout in seconds
        
        Returns:
            dict: Task data or None if timeout
        """
        # Blocking pop from queue (FIFO)
        result = self.r.brpop(QUEUE_NAME, timeout=timeout)
        
        if result is None:
            print("⏳ No tasks in queue (timeout)")
            return None
        
        _, task_json = result
        task = json.loads(task_json)
        url = task["url"]
        
        task_token = task.get("token")
        if task_token:
            lock_key = self._get_lock_key(url)
            if self.r.get(lock_key) != task_token:
                print(f"⚠️ Lock token mismatch for {url[:50]}... skipping")
                return None
        elif not self.acquire_lock(url, worker_token, ttl=3600):
            print(f"⚠️ Could not acquire lock for {url[:50]}... (another worker got it)")
            self.r.lpush(QUEUE_NAME, task_json)
            return None
        
        print(f"🎯 Task pulled from queue: {url[:50]}...")
        task["worker_token"] = worker_token
        task["token"] = task_token or worker_token
        task["started_at"] = datetime.now().isoformat()
        
        return task
    
    def complete_task(self, url, worker_token, result_data):
        """
        Mark task as complete and release lock
        
        Args:
            url: URL that was processed
            worker_token: Worker's unique token
            result_data: Analysis results
        """
        # Store result (optional - could use another Redis key)
        result_key = f"GEO:Result:{self._get_url_hash(url)}"
        result_data["completed_at"] = datetime.now().isoformat()
        self.r.setex(result_key, 86400, json.dumps(result_data))  # Expire after 24h
        
        # Release lock
        self.release_lock(url, worker_token)
        print(f"✅ Task completed: {url[:50]}...")
    
    def get_queue_length(self):
        """Get current queue length"""
        return self.r.llen(QUEUE_NAME)
    
    def get_active_locks(self):
        """Get all active locks"""
        pattern = f"{LOCK_PREFIX}:*"
        locks = self.r.keys(pattern)
        return [lock for lock in locks]


def test_producer():
    """Test: Producer (pushes URLs to queue)"""
    print("\n" + "="*50)
    print("📝 TEST: Producer (Push to Queue)")
    print("="*50)
    
    client = RedisGEOClient(REDIS_HOST, REDIS_PORT, REDIS_PASSWORD)
    
    # Test URLs
    test_urls = [
        "https://example.com/page1",
        "https://example.com/page2",
        "https://example.com/page1",  # Duplicate - should be skipped
        "https://example.com/page3",
    ]
    
    for i, url in enumerate(test_urls):
        print(f"\n{i+1}. Pushing: {url}")
        pushed = client.push_to_queue(url, priority=i)
        print(f"   Result: {'Pushed' if pushed else 'Skipped'}")
    
    print(f"\n📊 Queue length: {client.get_queue_length()}")


def test_consumer():
    """Test: Consumer (pulls and processes from queue)"""
    print("\n" + "="*50)
    print("🎯 TEST: Consumer (Pull from Queue)")
    print("="*50)
    
    client = RedisGEOClient(REDIS_HOST, REDIS_PORT, REDIS_PASSWORD)
    worker_token = f"worker_{int(time.time())}"
    
    print(f"Worker token: {worker_token}")
    print("Waiting for tasks... (timeout: 10s)\n")
    
    # Process up to 3 tasks
    for i in range(3):
        task = client.pull_from_queue(worker_token, timeout=10)
        
        if task is None:
            print("No more tasks available.")
            break
        
        # Simulate processing
        print(f"\nProcessing: {task['url']}")
        print(f"  Priority: {task['priority']}")
        print(f"  Enqueued: {task['enqueued_at']}")
        print(f"  Started: {task['started_at']}")
        
        # Simulate work
        time.sleep(2)
        
        # Complete task
        result = {
            "status": "success",
            "analysis": f"Completed analysis for {task['url']}",
            "worker": worker_token
        }
        client.complete_task(task['url'], task.get('token', worker_token), result)
    
    print(f"\n📊 Remaining queue length: {client.get_queue_length()}")


def test_distributed_locks():
    """Test: Distributed lock behavior"""
    print("\n" + "="*50)
    print("🔒 TEST: Distributed Locks")
    print("="*50)
    
    client = RedisGEOClient(REDIS_HOST, REDIS_PORT, REDIS_PASSWORD)
    
    test_url = "https://example.com/test-lock"
    worker1 = "worker_A"
    worker2 = "worker_B"
    
    print(f"\nURL: {test_url}")
    
    # Worker A acquires lock
    print(f"\n1. Worker A tries to acquire lock...")
    lock_a = client.acquire_lock(test_url, worker1, ttl=60)
    print(f"   Result: {'Acquired' if lock_a else 'Failed'}")
    
    # Worker B tries to acquire same lock
    print(f"\n2. Worker B tries to acquire same lock...")
    lock_b = client.acquire_lock(test_url, worker2, ttl=60)
    print(f"   Result: {'Acquired' if lock_b else 'Failed (expected)'}")
    
    # Worker B tries to release (should fail - token mismatch)
    print(f"\n3. Worker B tries to release lock...")
    released = client.release_lock(test_url, worker2)
    print(f"   Result: {'Released' if released else 'Failed (expected - token mismatch)'}")
    
    # Worker A releases (should succeed)
    print(f"\n4. Worker A tries to release lock...")
    released = client.release_lock(test_url, worker1)
    print(f"   Result: {'Released' if released else 'Failed'}")
    
    # Check active locks
    active_locks = client.get_active_locks()
    print(f"\n🔍 Active locks: {len(active_locks)}")
    for lock in active_locks:
        print(f"   - {lock}")


def show_connection_info():
    """Show Redis connection info"""
    print("\n" + "="*50)
    print("🔌 Redis Connection Info")
    print("="*50)
    print(f"Host: {REDIS_HOST}")
    print(f"Port: {REDIS_PORT}")
    print(f"Password: {REDIS_PASSWORD[:10]}...")
    print(f"Queue: {QUEUE_NAME}")
    print(f"Failed Queue: {FAILED_QUEUE_NAME}")
    print(f"Dead Letter Queue: {DEAD_LETTER_QUEUE_NAME}")
    print("\nTo update Redis IP after Terraform apply:")
    print("1. Run: terraform output redis_ip")
    print("2. Update REDIS_HOST in this script")


if __name__ == "__main__":
    show_connection_info()
    
    # Choose test to run
    import sys
    
    if len(sys.argv) > 1:
        test_name = sys.argv[1]
        if test_name == "producer":
            test_producer()
        elif test_name == "consumer":
            test_consumer()
        elif test_name == "locks":
            test_distributed_locks()
        else:
            print(f"Unknown test: {test_name}")
            print("Usage: python redis_test.py [producer|consumer|locks]")
    else:
        print("\nUsage: python redis_test.py [producer|consumer|locks]")
        print("\nExamples:")
        print("  python redis_test.py producer   # Push URLs to queue")
        print("  python redis_test.py consumer   # Pull and process URLs")
        print("  python redis_test.py locks      # Test distributed locks")
