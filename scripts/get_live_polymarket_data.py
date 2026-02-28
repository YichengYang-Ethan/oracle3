#!/usr/bin/env python
import argparse
import asyncio
import os
import sys
from datetime import datetime

# Adjust path to import your modules
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from oracle3.data.live.live_data_source import LivePolyMarketDataSource
from oracle3.events.events import NewsEvent, OrderBookEvent


async def test_polymarket_source(args):
    data_dir = args.data_dir
    os.makedirs(data_dir, exist_ok=True)

    # Create the data source
    data_source = LivePolyMarketDataSource(
        event_cache_file=os.path.join(data_dir, 'polymarket_events_cache.jsonl'),
        polling_interval=args.interval,
    )

    print(f'Starting LivePolyMarketDataSource (polling every {args.interval} seconds)')
    await data_source.start()

    # Event counters
    events_processed = 0
    order_book_events = 0
    news_events = 0

    print('Waiting for events...')

    # Set up a timeout or event limit
    start_time = datetime.now()
    duration = args.duration
    max_events = args.max_events

    while True:
        # Check if we've reached our limits
        elapsed = (datetime.now() - start_time).total_seconds()
        if (duration > 0 and elapsed > duration) or (
            max_events > 0 and events_processed >= max_events
        ):
            break

        try:
            event = await data_source.get_next_event()

            if event:
                events_processed += 1

                if isinstance(event, OrderBookEvent):
                    order_book_events += 1
                    print(
                        f'Order Book Event ({events_processed}): {event.ticker.symbol} @ {event.price}'
                    )
                    print(f'  Size: {event.size}, Delta: {event.size_delta}')

                elif isinstance(event, NewsEvent):
                    news_events += 1
                    print(f'News Event ({events_processed}): {event.title}')
                    if args.verbose:
                        print(f'  Content: {event.news}')

                if events_processed % 10 == 0:
                    print(
                        f'Processed {events_processed} events ({order_book_events} order book, {news_events} news)'
                    )

            else:
                # No event available, brief pause
                await asyncio.sleep(0.1)

        except KeyboardInterrupt:
            print('\nTest interrupted by user')
            break
        except Exception as e:
            print(f'Error: {e}')
            await asyncio.sleep(1.0)

    print('\nTest complete')
    print(
        f'Processed {events_processed} events ({order_book_events} order book, {news_events} news)'
    )
    print(f'Duration: {elapsed:.1f} seconds')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Test LivePolyMarketDataSource')
    parser.add_argument(
        '--interval', type=float, default=30.0, help='Polling interval in seconds'
    )
    parser.add_argument(
        '--data-dir', type=str, default='data', help='Directory to store data'
    )
    parser.add_argument(
        '--duration',
        type=int,
        default=300,
        help='Test duration in seconds (0 for unlimited)',
    )
    parser.add_argument(
        '--max-events',
        type=int,
        default=100,
        help='Maximum number of events to process (0 for unlimited)',
    )
    parser.add_argument(
        '--verbose', action='store_true', help='Show detailed event information'
    )

    args = parser.parse_args()

    try:
        asyncio.run(test_polymarket_source(args))
    except KeyboardInterrupt:
        print('\nScript interrupted')
