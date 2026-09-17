import time


if __name__ == "__main__":
    stages = [
        "Agent started",
        "Reading files...",
        "Analyzing task...",
        "Writing changes...",
        "Running tests...",
        "Agent finished",
    ]

    for index, stage in enumerate(stages):
        if index > 0:
            time.sleep(2)
        print(stage, flush=True)
