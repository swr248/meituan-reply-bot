from auth_ticket import consume_ticket, issue_ticket, verify_ticket


def test_ticket_is_scoped_and_expires():
    ticket = issue_ticket('secret', 'shop1', 'browser', now=100, ttl=60)
    assert verify_ticket(ticket, 'secret', 'shop1', target='browser', now=160)['target'] == 'browser'
    for secret, shop, now in [('wrong', 'shop1', 120), ('secret', 'shop2', 120), ('secret', 'shop1', 161)]:
        try:
            verify_ticket(ticket, secret, shop, now=now)
            assert False, 'ticket must be rejected'
        except ValueError:
            pass


def test_ticket_rejects_wrong_target():
    ticket = issue_ticket('secret', 'shop1', 'browser', now=100, ttl=60)
    try:
        verify_ticket(ticket, 'secret', 'shop1', target='admin', now=120)
        assert False, 'wrong target ticket must be rejected'
    except ValueError:
        pass


def test_ticket_can_only_be_consumed_once():
    ticket = issue_ticket('secret', 'shop1', 'admin', now=100, ttl=60)
    assert consume_ticket(ticket, 'secret', 'shop1', target='admin', now=120)['jti']
    try:
        consume_ticket(ticket, 'secret', 'shop1', target='admin', now=121)
        assert False, 'replayed ticket must be rejected'
    except ValueError:
        pass


def test_ticket_rejects_future_issue_time_and_excessive_ttl():
    future = issue_ticket('secret', 'shop1', 'admin', now=200, ttl=60)
    too_long = issue_ticket('secret', 'shop1', 'admin', now=100, ttl=600)
    for ticket in (future, too_long):
        try:
            verify_ticket(ticket, 'secret', 'shop1', target='admin', now=100)
            assert False, 'unsafe ticket lifetime must be rejected'
        except ValueError:
            pass
