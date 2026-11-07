"""Маленькие статистические функции."""


def mean(numbers):
    return sum(numbers) / len(numbers)


def median(numbers):
    ordered = sorted(numbers)
    middle = len(ordered) // 2
    if len(ordered) % 2 == 0:
        return (ordered[middle - 1] + ordered[middle]) / 2
    return ordered[middle]


def clamp(value, low, high):
    return max(low, min(value, high))
