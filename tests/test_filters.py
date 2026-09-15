"""Regression checks for raffle wording in official community posts."""

import unittest

from utils.api import HoyolabPost
from utils.filters import is_raffle_post


def post(title: str, snippet: str) -> HoyolabPost:
    return HoyolabPost('1', 1, title, snippet, 'https://www.hoyolab.com/article/1',
                       'Honkai Impact 3rd', True, None, None, None, 1)


class RaffleFilterTests(unittest.TestCase):
    def test_sirin_time_album_giveaway(self):
        self.assertTrue(is_raffle_post(post(
            'Magical Girl Sirin, go!',
            "Today's Time Album Spotlight: Sirin\n"
            "Today's Topic: If Sirin's scissors could help you, what would you like to cut away?\n"
            'Leave a like and comment for a chance to win a $100 Gift Card, '
            'a merch bundle, and more!')))

    def test_bare_reward_mentions_do_not_match(self):
        self.assertFalse(is_raffle_post(post('New merch bundle',
                                            'Explore the new $100 gift card and merch bundle.')))


if __name__ == '__main__':
    unittest.main()
