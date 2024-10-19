import asyncio
#from shootfuture_sepe_TPSL import place_futures_order
from shootspotnfuture import place_futures_order
import aiohttp

# Your main function to execute the async futures order placement
async def main():
    session = aiohttp.ClientSession()

    await place_futures_order(
        account_name="SOLNormal",  # Account name to fetch API credentials
        symbol="SOLUSDT",  # The trading pair
        side="Sell",  # 'Buy' or 'Sell'
        qty=0.5,  # Quantity of the asset
        leverage=10,  # Leverage amount
        tp_type='percentage',  # TP as a percentage of entry price
        tp_value=0.2,  # Take Profit 2% above the entry price
        sl_type='percentage',  # SL as a percentage of entry price
        sl_value=1.0,  # Stop Loss 1% below the entry price
        session=session,
        trigger_by='LastPrice'  # Trigger by LastPrice
    )

    await session.close()

# Execute the async function
if __name__ == "__main__":
    asyncio.run(main())
