"""Strict all-account bill pagination, offline mock read() only."""
from copy import deepcopy
import unittest
from unittest.mock import Mock
from livecore.okx import OKXClient, ProtocolError, SafetyError

NOW=2000000000000


def bill(i,**changes):
    result={'billId':str(i),'ts':str(NOW-1000),'type':'1','subType':'11','ccy':'USDT','balChg':'1'}
    result.update(changes)
    return result


class CapitalAPITests(unittest.TestCase):
    def client(self,*pages):
        client=OKXClient(transport=lambda *_args,**_kw: self.fail('no transport expected'))
        client.now=lambda: NOW
        client.read=Mock(side_effect=list(pages))
        return client
    def test_signed_unfiltered_fixed_begin_end_and_descending_cursor(self):
        first=[bill(i) for i in range(200,100,-1)]
        client=self.client(first,[bill(100)])
        rows=client.account_bills(NOW-10000,NOW)
        self.assertEqual(len(rows),101)
        first_call=client.read.call_args_list[0]
        self.assertEqual(first_call.args[0],'/api/v5/account/bills')
        self.assertEqual(first_call.args[1],{'begin':str(NOW-10000),'end':str(NOW),'limit':'100'})
        self.assertEqual(first_call.kwargs,{'private':True})
        self.assertEqual(client.read.call_args_list[1].args[1]['after'],'101')
    def test_duplicate_conflict_across_page_boundary_rejected(self):
        first=[bill(i) for i in range(200,100,-1)]
        client=self.client(first,[bill(101,balChg='2'),bill(100)])
        with self.assertRaises(ProtocolError): client.account_bills(NOW-10000,NOW)
    def test_exact_overlap_is_deduplicated_with_progress(self):
        first=[bill(i) for i in range(200,100,-1)]
        client=self.client(first,[bill(101),bill(100)])
        self.assertEqual(len(client.account_bills(NOW-10000,NOW)),101)
    def test_page_overflow_bad_order_and_no_progress_rejected(self):
        for pages in (([bill(i) for i in range(300,199,-1)],),([bill(1),bill(2)],),
                      ([bill(i) for i in range(200,100,-1)],[bill(101)])):
            with self.subTest(pages=len(pages)):
                client=self.client(*pages)
                with self.assertRaises(ProtocolError): client.account_bills(NOW-10000,NOW)
    def test_invalid_and_over_retention_windows_rejected_before_read(self):
        for begin,end in ((True,NOW),(NOW+1,NOW),(NOW-7*86400000,NOW),(NOW,NOW+1)):
            client=self.client()
            with self.assertRaises(SafetyError): client.account_bills(begin,end)
            client.read.assert_not_called()
    def test_out_of_range_timestamp_invalid_number_and_empty_currency_rejected(self):
        for changes in ({'ts':str(NOW+1)},{'balChg':'NaN'},{'ccy':''},{'billId':'-3'},{'type':True}):
            client=self.client([bill(1,**changes)])
            with self.assertRaises(ProtocolError): client.account_bills(NOW-10000,NOW)
    def test_read_failure_never_returns_partial_first_page(self):
        client=self.client([bill(i) for i in range(200,100,-1)],OSError('offline'))
        with self.assertRaises(OSError): client.account_bills(NOW-10000,NOW)
    def test_page_limit_rejected(self):
        pages=[[bill(j) for j in range(20000-100*i,19900-100*i,-1)] for i in range(100)]
        client=self.client(*pages)
        with self.assertRaises(ProtocolError): client.account_bills(NOW-10000,NOW)


if __name__=='__main__': unittest.main()
