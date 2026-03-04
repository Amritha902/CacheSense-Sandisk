import os
import random
import string

DIR = "mountpoint"
FILES = 10000

os.makedirs(DIR, exist_ok=True)

for i in range(FILES):
    name = f"{DIR}/file_{i}.txt"
    data = ''.join(random.choices(string.ascii_letters, k=4096))

    with open(name, "w") as f:
        f.write(data)

    if i % 1000 == 0:
        print("written", i)

print("done")
