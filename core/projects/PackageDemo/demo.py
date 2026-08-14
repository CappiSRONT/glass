"""A tiny Glass package: real Python a .glass UI can call."""
import time

def run(ctx):
    ctx.set("Status", "hello from Python!")
    ctx.log("run() was called from a button")

def add(ctx):
    ctx.set("Clicks", ctx.get("Clicks", 0) + 1)

def slow(ctx):
    # demonstrate background work that updates the UI as it finishes
    ctx.set("Status", "working...")
    def job():
        time.sleep(1.0)
        ctx.set("Status", "done!")
    ctx.thread(job)
