"""
gunicorn.conf.py — Gunicorn production configuration for EC2
Run with: gunicorn -c gunicorn.conf.py app:app
"""
import multiprocessing

# Bind to all interfaces on port 5000 (Nginx will proxy from port 80)
bind = "0.0.0.0:5000"

# Workers = (2 × CPU cores) + 1
workers = multiprocessing.cpu_count() * 2 + 1

# Worker type (sync is fine for this app; use gevent if you add long-polling)
worker_class = "sync"

# Timeout for slow requests (e.g. large image uploads to S3)
timeout = 120

# Keep-alive connections
keepalive = 5

# Logging
accesslog = "/var/log/campus-tracker/access.log"
errorlog  = "/var/log/campus-tracker/error.log"
loglevel  = "info"

# Restart workers after this many requests (prevents memory leaks)
max_requests = 1000
max_requests_jitter = 100

# Preload app for faster worker spawning
preload_app = True
