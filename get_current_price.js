#!/usr/bin/env node
/**
 * Quick script to get current price from Uniswap pool.
 * Returns JSON with current price.
 */

import 'dotenv/config'
import { poolById, getPoolHourData } from './uniPoolData.mjs'

const poolID = process.argv[2];
const priceToken = process.argv[3] ? parseInt(process.argv[3]) : 0;
const protocol = process.argv[4] ? parseInt(process.argv[4]) : 0;

if (!poolID) {
  console.error('Usage: node get_current_price.js <poolID> [priceToken] [protocol]');
  process.exit(1);
}

try {
  // Get pool data
  const poolData = await poolById(poolID, protocol);
  
  if (!poolData) {
    console.error(JSON.stringify({ error: 'Failed to fetch pool data' }));
    process.exit(1);
  }
  
  // Try to get current price from pool data (token1Price for priceToken=0)
  let currentPrice = null;
  
  if (priceToken === 0 && poolData.token1Price) {
    // Price in terms of token0 (USDC), so token1Price is the price we want
    currentPrice = parseFloat(poolData.token1Price);
  } else if (priceToken === 1 && poolData.token0Price) {
    // Price in terms of token1, so token0Price (inverse)
    currentPrice = 1 / parseFloat(poolData.token0Price);
  }
  
  // If not available from pool data, get from latest hourly data
  if (!currentPrice) {
    const now = Math.floor(Date.now() / 1000);
    const oneHourAgo = now - 3600;
    const hourlyData = await getPoolHourData(poolID, oneHourAgo, now, protocol);
    
    if (hourlyData && hourlyData.length > 0) {
      // Get the most recent entry (should be last one)
      const latest = hourlyData[hourlyData.length - 1];
      currentPrice = priceToken === 1 ? (1 / parseFloat(latest.close)) : parseFloat(latest.close);
    }
  }
  
  // Fallback to poolDayData if available
  if (!currentPrice && poolData.poolDayData && poolData.poolDayData.length > 0) {
    const latestDay = poolData.poolDayData[0];
    currentPrice = priceToken === 1 ? (1 / parseFloat(latestDay.close)) : parseFloat(latestDay.close);
  }
  
  if (!currentPrice) {
    console.error(JSON.stringify({ error: 'Could not determine current price' }));
    process.exit(1);
  }
  
  console.log(JSON.stringify({
    currentPrice: currentPrice,
    poolID: poolID,
    priceToken: priceToken
  }));
  
} catch (error) {
  console.error(JSON.stringify({ error: error.message }));
  process.exit(1);
}
