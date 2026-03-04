import matplotlib.pyplot as plt

# Data from workload_log.txt
labels = ["RAW", "Compressed"]
values = [165076, 135424]

plt.figure()
plt.bar(labels, values)
plt.title("Block Distribution")
plt.ylabel("Blocks")
plt.savefig("graphs/block_distribution.png")


metrics = ["Throughput MB/s", "Blocks/sec"]
values = [2.19, 561]

plt.figure()
plt.bar(metrics, values)
plt.title("Performance Metrics")
plt.savefig("graphs/performance.png")


cache = ["Hit", "Miss"]
values = [45.07, 54.93]

plt.figure()
plt.pie(values, labels=cache, autopct='%1.1f%%')
plt.title("Cache Hit Rate")
plt.savefig("graphs/cache_hit_rate.png")

print("Graphs generated in graphs/")
