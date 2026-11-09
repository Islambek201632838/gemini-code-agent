import unittest

from stats import clamp, mean, median


class TestStats(unittest.TestCase):
    def test_mean(self):
        self.assertEqual(mean([1, 2, 3]), 2)

    def test_mean_single_element(self):
        self.assertEqual(mean([5]), 5)

    def test_mean_negative_numbers(self):
        self.assertEqual(mean([-10, -20, -30]), -20)

    def test_mean_floats(self):
        self.assertEqual(mean([1.5, 2.5, 5.0]), 3.0)

    def test_mean_empty_raises_zero_division(self):
        with self.assertRaises(ZeroDivisionError):
            mean([])

    def test_median_odd(self):
        self.assertEqual(median([3, 1, 2]), 2)

    def test_median_even(self):
        self.assertEqual(median([4, 1, 3, 2]), 2.5)

    def test_median_single_element(self):
        self.assertEqual(median([7]), 7)

    def test_median_negative_and_floats(self):
        self.assertEqual(median([-3.5, 1.5, -0.5]), -0.5)
        self.assertEqual(median([-2.0, -1.0, -4.0, -3.0]), -2.5)

    def test_median_empty_raises_index_error(self):
        with self.assertRaises(IndexError):
            median([])

    def test_clamp_inside_range(self):
        self.assertEqual(clamp(5, 0, 10), 5)

    def test_clamp_above_high(self):
        self.assertEqual(clamp(15, 0, 10), 10)

    def test_clamp_below_low(self):
        self.assertEqual(clamp(-5, 0, 10), 0)

    def test_clamp_low_equal_high(self):
        self.assertEqual(clamp(5, 10, 10), 10)

    def test_clamp_low_greater_than_high(self):
        self.assertEqual(clamp(7, 10, 5), 10)


if __name__ == "__main__":
    unittest.main()
