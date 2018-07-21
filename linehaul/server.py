# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import itertools
import logging
import uuid

from functools import partial
from typing import Optional

import arrow
import cattr
import tenacity
import trio

from linehaul.events import parser as _event_parser
from linehaul.protocol import LineReceiver, BufferTooLargeError, TruncatedLineError
from linehaul.syslog import parser as _syslog_parser
from linehaul.logging import SPEW as log_SPEW


logger = logging.getLogger(__name__)


retry = partial(tenacity.retry, sleep=trio.sleep)


_cattr = cattr.Converter()
_cattr.register_unstructure_hook(arrow.Arrow, lambda o: o.float_timestamp)


#
# Non I/O Functions
#


def parse_line(line: bytes, token=None) -> Optional[_event_parser.Download]:
    line = line.decode("utf8")

    # Check our token, and remove it from the start of the line if it matches.
    if token is not None:
        # TODO: Use a Constant Time Compare?
        if not line.startswith(token):
            return
        line = line[len(token) :]

    # Parse the incoming Syslog Message, and get the download event out of it.
    try:
        msg = _syslog_parser.parse(line)
        event = _event_parser.parse(msg.message)
    except ValueError:
        # TODO: Better Error Logging.
        return

    return event


def extract_item_date(item):
    return item.timestamp.format("YYYYMDDD")


def compute_batches(all_items):
    for date, items in itertools.groupby(
        sorted(all_items, key=extract_item_date), extract_item_date
    ):
        items = list(items)

        yield extract_item_date(items[0]), [
            {"insertId": str(uuid.uuid4()), "json": row}
            for row in _cattr.unstructure(items)
        ],


#
# I/O Functions
#


async def handle_connection(stream, q, token=None, max_line_size=None, recv_size=None):
    if recv_size is None:
        recv_size = 8192

    # Sometimes if a connection is open, and closes really fast, we can call this
    # after the peer has disconnected, making this an error. In those cases we'll
    # just log what we have.
    peer_id = uuid.uuid4()
    try:
        peer, *_ = stream.socket.getpeername()
    except OSError:
        peer = "Unknown"
    logger.debug("{%s}: Connection received from %r.", peer_id, peer)

    lr = LineReceiver(partial(parse_line, token=token), max_line_size=max_line_size)

    try:
        while True:
            try:
                data: bytes = await stream.receive_some(recv_size)
            except trio.BrokenStreamError:
                data = b""

            for event in lr.recieve_data(data):
                logger.log(log_SPEW, "{%s}: Received Event: %r", peer_id, event)
                await q.put(event)

            if not data:
                logger.debug("{%s}: Connection lost from %r.", peer_id, peer)
                lr.close()
                break
    except BufferTooLargeError:
        logger.debug("{%s}: Buffer too large; Dropping connection.", peer_id)
    except TruncatedLineError as exc:
        logger.debug("{%s}: Truncated line %r; Dropping connection.", peer_id, exc.line)
    finally:
        with trio.move_on_after(30):
            await stream.aclose()


@retry(
    retry=tenacity.retry_if_exception_type((trio.TooSlowError, trio.BrokenStreamError)),
    reraise=True,
    before_sleep=tenacity.before_sleep_log(logger, logging.WARNING),
)
async def actually_send_batch(bq, table, template_suffix, batch, api_timeout=None):
    if api_timeout is None:
        api_timeout = 15

    with trio.fail_after(api_timeout):
        await bq.insert_all(table, batch, template_suffix)


async def send_batch(
    bq,
    table,
    template_suffix,
    batch,
    *args,
    retry_max_attempts=None,
    retry_max_wait=None,
    retry_multiplier=None,
    **kwargs
):
    if retry_max_attempts is None:
        retry_max_attempts = 15
    if retry_max_wait is None:
        retry_max_wait = 60
    if retry_multiplier is None:
        retry_multiplier = 0.5

    # We split up send_batch and actually_send_batch so that we can use tenacity to
    # handle retries for us, while still getting to use the Nurser.start_soon interface.
    # This also makes it easier to deal with the error handling aspects of sending a
    # batch, from the work of actually sending. The general rule here is that errors
    # shoudl not escape from this function.

    send = actually_send_batch.retry_with(
        wait=tenacity.wait_exponential(multiplier=retry_multiplier, max=retry_max_wait),
        stop=tenacity.stop_after_attempt(retry_max_attempts),
    )

    try:

        await send(bq, table, template_suffix, batch, *args, **kwargs)
    # We've tried to send this batch to BigQuery, however for one reason or another
    # we were unable to do so. We should log this error, but otherwise we're going
    # to just drop this on the floor because there's not much else we can do here
    # except buffer it forever (which is not a great idea).
    except trio.TooSlowError:
        logger.error("Timed out sending %d items; Dropping them.", len(batch))
    except Exception:
        logger.exception("Error sending %d items; Dropping them.", len(batch))


async def sender(
    bq,
    table,
    q,
    *,
    batch_size=None,
    batch_timeout=None,
    retry_max_attempts=None,
    retry_max_wait=None,
    retry_multiplier=None,
    api_timeout=None
):
    if batch_size is None:
        batch_size = 500
    if batch_timeout is None:
        batch_timeout = 30

    async with trio.open_nursery() as nursery:
        while True:
            batch = []
            with trio.move_on_after(batch_timeout) as cancel_scope:
                while len(batch) < batch_size:
                    batch.append(await q.get())

            if batch and cancel_scope.cancelled_caught:
                logger.debug("Batch timed out; Sending %d items.", len(batch))
            elif batch:
                logger.debug("%d items accumulated, sending batch.", len(batch))

            for template_suffix, batch in compute_batches(batch):
                nursery.start_soon(
                    partial(
                        send_batch,
                        bq,
                        table,
                        template_suffix,
                        batch,
                        retry_max_attempts=retry_max_attempts,
                        retry_max_wait=retry_max_wait,
                        retry_multiplier=retry_multiplier,
                        api_timeout=api_timeout,
                    )
                )


#
# Main Entry point
#


async def server(
    bq,
    table,
    bind="0.0.0.0",
    port=512,
    token=None,
    max_line_size=None,
    recv_size=None,
    qsize=10000,
    batch_size=None,
    batch_timeout=None,
    retry_max_attempts=None,
    retry_max_wait=None,
    retry_multiplier=None,
    api_timeout=None,
    task_status=trio.TASK_STATUS_IGNORED,
):
    # Total number of buffered events is:
    #       qsize + (COUNT(send_batch) * batch_size)
    # However, the length of time a single send_batch call sticks around for is time
    # boxed, so this won't grow forever. It will not however, apply any backpressure
    # to the sender (we can't meaningfully apply backpressure, since these are download
    # events being streamed to us).
    q = trio.Queue(qsize)

    async with trio.open_nursery() as nursery:
        nursery.start_soon(
            partial(
                sender,
                bq,
                table,
                q,
                batch_size=batch_size,
                batch_timeout=batch_timeout,
                retry_max_attempts=retry_max_attempts,
                retry_max_wait=retry_max_wait,
                retry_multiplier=retry_multiplier,
                api_timeout=api_timeout,
            )
        )

        await nursery.start(
            trio.serve_tcp,
            partial(
                handle_connection,
                q=q,
                token=token,
                max_line_size=max_line_size,
                recv_size=recv_size,
            ),
            port,
        )

        logging.info("Listening on %s:%d and sending to %r", bind, port, table)
        task_status.started()
