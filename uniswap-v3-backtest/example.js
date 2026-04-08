import 'dotenv/config'
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';
import uniswapStrategyBacktest from './index.js';
import { poolById } from './uniPoolData.mjs';
import { tokensForStrategy, liquidityForStrategy, tokensFromLiquidity } from './backtest.mjs';

// Get directory path for ES modules
const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const rootDir = path.resolve(__dirname, '..');

// Parse command-line arguments
function parseArgs() {
  const args = process.argv.slice(2);
  const parsed = {
    jsonOnly: false
  };
  
  for (let i = 0; i < args.length; i++) {
    const arg = args[i];
    if (arg === '--json-only') {
      parsed.jsonOnly = true;
    } else if (arg.startsWith('--')) {
      const key = arg.slice(2);
      const value = args[i + 1];
      if (value && !value.startsWith('--')) {
        parsed[key] = value;
        i++; // Skip next arg as it's the value
      } else {
        parsed[key] = true;
      }
    }
  }
  
  return parsed;
}

const cliArgs = parseArgs();
const jsonOnly = cliArgs.jsonOnly;

// Load configuration from config.json
let config = {};
try {
  const configFile = fs.readFileSync('./config.json', 'utf8');
  config = JSON.parse(configFile);
} catch (error) {
  if (!jsonOnly) {
  console.error("❌ Error loading config.json:", error.message);
  console.error("   Make sure config.json exists in the same directory");
  }
  // If JSON only, we might have all args from CLI, so continue
  if (!cliArgs['pool-id']) {
  process.exit(1);
}
}

// Extract configuration values (CLI args override config.json)
const poolID = cliArgs['pool-id'] || config.poolID;
const investmentAmount = cliArgs['investment'] ? parseFloat(cliArgs['investment']) : config.investmentAmount;
const minRange = cliArgs['min-range'] ? parseFloat(cliArgs['min-range']) : config.minRange;
const maxRange = cliArgs['max-range'] ? parseFloat(cliArgs['max-range']) : config.maxRange;
const days = cliArgs['days'] ? parseInt(cliArgs['days']) : (config.days || 30);
const period = cliArgs['period'] || config.period || "daily";
const protocol = cliArgs['protocol'] ? parseInt(cliArgs['protocol']) : (config.protocol || 0);
const priceToken = cliArgs['price-token'] ? parseInt(cliArgs['price-token']) : (config.priceToken || 0);

// Validate required fields
if (!poolID || investmentAmount === undefined || minRange === undefined || maxRange === undefined) {
  if (!jsonOnly) {
    console.error("❌ Missing required fields:");
    console.error("   Required: --pool-id, --investment, --min-range, --max-range");
    console.error("   Or provide them in config.json");
  }
  process.exit(1);
}

if (!jsonOnly) {
console.log("Running Uniswap V3 Backtest...");
console.log(`Pool: ${poolID}`);
console.log(`Investment: $${investmentAmount}`);
console.log(`Price Range: $${minRange} - $${maxRange}`);
console.log(`Period: Last ${days} ${days === 1 ? 'day' : 'days'}, ${period === 'daily' ? 'Daily' : 'Hourly'} aggregation\n`);
}

try {
  // Get pool data to calculate initial tokens
  const poolData = await poolById(poolID, 0);
  
  // Update config file with token information
  if (poolData && poolData.token0 && poolData.token1) {
    config.token0 = {
      id: poolData.token0.id || null,
      symbol: poolData.token0.symbol || 'TOKEN0',
      name: poolData.token0.name || null,
      decimals: poolData.token0.decimals || '18'
    };
    config.token1 = {
      id: poolData.token1.id || null,
      symbol: poolData.token1.symbol || 'TOKEN1',
      name: poolData.token1.name || null,
      decimals: poolData.token1.decimals || '18'
    };
    
    // Save updated config with token information (only if not json-only mode)
    if (!jsonOnly) {
    try {
      fs.writeFileSync('./config.json', JSON.stringify(config, null, 2));
      console.log(`📝 Config updated with token information: ${config.token0.symbol}/${config.token1.symbol}\n`);
    } catch (error) {
      console.log(`⚠️  Could not update config file: ${error.message}\n`);
      }
    }
  }
  
  const results = await uniswapStrategyBacktest(
    poolID,
    investmentAmount,
    minRange,
    maxRange,
    { 
      days: days,
      period: period,
      protocol: protocol,
      priceToken: priceToken
    }
  );

  if (results && results.length > 0) {
    if (!jsonOnly) {
    console.log(`✅ Backtest completed! Processed ${results.length} periods\n`);
    
    // Show first and last results
    console.log("First day result:");
    console.log(JSON.stringify(results[0], null, 2));
    console.log("\nLast day result:");
    console.log(JSON.stringify(results[results.length - 1], null, 2));
    }
    
    // Calculate summary
    const totalFeesToken0 = results.reduce((sum, r) => sum + (r.feeToken0 || 0), 0);
    const totalFeesToken1 = results.reduce((sum, r) => sum + (r.feeToken1 || 0), 0);
    const totalFeesUSD = results.reduce((sum, r) => sum + (r.feeUSD || 0), 0);
    const avgActiveLiquidity = results.reduce((sum, r) => sum + (r.activeliquidity || 0), 0) / results.length;
    const finalValue = results[results.length - 1].amountV || investmentAmount;
    const activeDays = results.filter(r => (r.activeliquidity || 0) > 0).length;

    // Calculate HODL comparison
    // Get entry and final prices
    const entryPrice = parseFloat(results[0].baseClose || results[0].close);
    const finalPrice = parseFloat(results[results.length - 1].baseClose || results[results.length - 1].close);
    
    // Calculate initial token allocation at entry price
    // Use the same method as the backtest function (same as index.js lines 31-32)
    const decimalDiff = parseInt(poolData.token1.decimals) - parseInt(poolData.token0.decimals);
    const tokens = tokensForStrategy(minRange, maxRange, investmentAmount, entryPrice, decimalDiff);
    const liquidity = liquidityForStrategy(entryPrice, minRange, maxRange, tokens[0], tokens[1], poolData.token0.decimals, poolData.token1.decimals);
    
    // Now use tokensFromLiquidity to get the actual token amounts (same as backtest.mjs line 139)
    const initialTokens = tokensFromLiquidity(entryPrice, minRange, maxRange, liquidity, poolData.token0.decimals, poolData.token1.decimals);
    const initialToken0 = initialTokens[0]; // USDC amount
    const initialToken1 = initialTokens[1]; // WETH amount
    
    // Calculate initial value to verify it matches investment
    // For ETH/USDC: value = USDC + (WETH * price)
    const initialValueCheck = initialToken0 + (initialToken1 * entryPrice);
    
    // Calculate actual period in days from timestamps
    const startTimestamp = results[0].periodStartUnix;
    const endTimestamp = results[results.length - 1].periodStartUnix;
    const actualDays = (endTimestamp - startTimestamp) / (24 * 60 * 60); // Convert seconds to days
    
    // Extract HODL value from amountTR field
    // amountTR = investment + (amountV - HODL_value)
    // So: HODL_value = investment + amountV - amountTR
    const lastResult = results[results.length - 1];
    const hodlValue = investmentAmount + finalValue - (lastResult.amountTR || investmentAmount);
    
    // Verify HODL calculation: should equal initial tokens valued at final price
    // HODL = initialToken0 + (initialToken1 * finalPrice)
    const hodlValueVerified = initialToken0 + (initialToken1 * finalPrice);
    
    // Use verified HODL value (more accurate)
    const hodlValueFinal = Math.abs(hodlValue - hodlValueVerified) < 1 ? hodlValueVerified : hodlValue;
    
    // Calculate fees value in USD terms
    // For ETH/USDC: token0 is USDC, token1 is ETH
    // Fee value = token0 fees + (token1 fees * final price)
    const feesValueInUSD = totalFeesToken0 + (totalFeesToken1 * finalPrice);
    
    // LP position + fees total
    const lpWithFees = finalValue + feesValueInUSD;
    
    // Calculate ROI and APY for LP Strategy
    const lpTotalReturn = lpWithFees - investmentAmount;
    const lpROI = (lpTotalReturn / investmentAmount) * 100;
    // APY = (Final Value / Initial Value) ^ (365 / actual days) - 1
    const lpAPY = actualDays > 0 ? (Math.pow(lpWithFees / investmentAmount, 365 / actualDays) - 1) * 100 : 0;
    
    // Calculate ROI and APY for HODL Strategy
    const hodlTotalReturn = hodlValueFinal - investmentAmount;
    const hodlROI = (hodlTotalReturn / investmentAmount) * 100;
    // APY = (Final Value / Initial Value) ^ (365 / actual days) - 1
    const hodlAPY = actualDays > 0 ? (Math.pow(hodlValueFinal / investmentAmount, 365 / actualDays) - 1) * 100 : 0;
    
    // Comparison metrics
    const roiDifference = lpROI - hodlROI;
    const apyDifference = lpAPY - hodlAPY;

    // Calculate period label based on actual time period
    const actualHours = actualDays * 24;
    let periodLabel, periodValue;
    if (period === 'daily') {
      periodValue = Math.round(actualDays);
      periodLabel = periodValue === 1 ? 'day' : 'days';
    } else {
      // For hourly, show days if > 24 hours, otherwise show hours
      if (actualHours >= 24) {
        periodValue = Math.round(actualDays * 10) / 10; // Round to 1 decimal
        periodLabel = periodValue === 1 ? 'day' : 'days';
      } else {
        periodValue = Math.round(actualHours);
        periodLabel = periodValue === 1 ? 'hour' : 'hours';
      }
    }
    const resultsLabel = period === 'daily' ? (results.length === 1 ? 'day' : 'days') : (results.length === 1 ? 'hour' : 'hours');
    
    if (!jsonOnly) {
    console.log("\n=== Summary ===");
    const periodDisplay = periodValue % 1 === 0 ? periodValue.toString() : periodValue.toFixed(1);
    console.log(`Backtest Period: Last ${periodDisplay} ${periodLabel} (${results.length} ${resultsLabel})`);
    console.log(`Average Active Liquidity: ${avgActiveLiquidity.toFixed(2)}%`);
    if (period === 'daily') {
      console.log(`Days Active: ${activeDays} out of ${results.length} ${resultsLabel}`);
    } else {
      console.log(`Periods Active: ${activeDays} out of ${results.length} ${resultsLabel}`);
    }
    
    console.log(`\n--- Initial Investment ---`);
    console.log(`Total Investment: $${investmentAmount.toFixed(2)}`);
    console.log(`Entry Price: $${entryPrice.toFixed(2)}`);
    console.log(`Initial ${poolData.token0.symbol}: ${initialToken0.toFixed(6)}`);
    console.log(`Initial ${poolData.token1.symbol}: ${initialToken1.toFixed(6)}`);
    
    console.log(`\n--- Fees Breakdown ---`);
    console.log(`Fees in ${poolData.token0.symbol}: ${totalFeesToken0.toFixed(6)}`);
    console.log(`Fees in ${poolData.token1.symbol}: ${totalFeesToken1.toFixed(6)}`);
    
    console.log(`\n--- LP Strategy Performance ---`);
    console.log(`ROI: ${lpROI > 0 ? '+' : ''}${lpROI.toFixed(2)}%`);
    console.log(`APY: ${lpAPY > 0 ? '+' : ''}${lpAPY.toFixed(2)}%`);
    
    console.log(`\n--- HODL Strategy Performance ---`);
    console.log(`Entry Price: $${entryPrice.toFixed(2)}`);
    console.log(`Final Price: $${finalPrice.toFixed(2)}`);
    console.log(`ROI: ${hodlROI > 0 ? '+' : ''}${hodlROI.toFixed(2)}%`);
    console.log(`APY: ${hodlAPY > 0 ? '+' : ''}${hodlAPY.toFixed(2)}%`);
    
    console.log(`\n--- Comparison: LP vs HODL ---`);
    console.log(`ROI Difference: ${roiDifference > 0 ? '+' : ''}${roiDifference.toFixed(2)}%`);
    console.log(`APY Difference: ${apyDifference > 0 ? '+' : ''}${apyDifference.toFixed(2)}%`);
    }
    
    // Prepare results data for JSON export
    const resultsData = {
      timestamp: new Date().toISOString(),
      config: {
        poolID: poolID,
        investmentAmount: investmentAmount,
        minRange: minRange,
        maxRange: maxRange,
        days: days,
        period: period,
        protocol: protocol,
        priceToken: priceToken,
        token0: config.token0 || null,
        token1: config.token1 || null
      },
      period: {
        actualDays: parseFloat(actualDays.toFixed(2)),
        actualHours: parseFloat(actualHours.toFixed(2)),
        periodsProcessed: results.length,
        startTimestamp: startTimestamp,
        endTimestamp: endTimestamp,
        startDate: new Date(startTimestamp * 1000).toISOString(),
        endDate: new Date(endTimestamp * 1000).toISOString()
      },
      pool: {
        id: poolData.id,
        token0: {
          id: poolData.token0?.id || null,
          symbol: poolData.token0?.symbol || 'TOKEN0',
          decimals: poolData.token0?.decimals || '18'
        },
        token1: {
          id: poolData.token1?.id || null,
          symbol: poolData.token1?.symbol || 'TOKEN1',
          decimals: poolData.token1?.decimals || '18'
        }
      },
      initialInvestment: {
        amount: investmentAmount,
        entryPrice: parseFloat(entryPrice.toFixed(2)),
        token0Amount: parseFloat(initialToken0.toFixed(6)),
        token1Amount: parseFloat(initialToken1.toFixed(6))
      },
      fees: {
        token0: parseFloat(totalFeesToken0.toFixed(6)),
        token1: parseFloat(totalFeesToken1.toFixed(6)),
        totalUSD: parseFloat(feesValueInUSD.toFixed(2))
      },
      lpStrategy: {
        finalValue: parseFloat(finalValue.toFixed(2)),
        totalValueWithFees: parseFloat(lpWithFees.toFixed(2)),
        totalReturn: parseFloat(lpTotalReturn.toFixed(2)),
        roi: parseFloat(lpROI.toFixed(2)),
        apy: parseFloat(lpAPY.toFixed(2)),
        avgActiveLiquidity: parseFloat(avgActiveLiquidity.toFixed(2)),
        activePeriods: activeDays
      },
      hodlStrategy: {
        entryPrice: parseFloat(entryPrice.toFixed(2)),
        finalPrice: parseFloat(finalPrice.toFixed(2)),
        hodlValue: parseFloat(hodlValueFinal.toFixed(2)),
        totalReturn: parseFloat(hodlTotalReturn.toFixed(2)),
        roi: parseFloat(hodlROI.toFixed(2)),
        apy: parseFloat(hodlAPY.toFixed(2))
      },
      comparison: {
        roiDifference: parseFloat(roiDifference.toFixed(2)),
        apyDifference: parseFloat(apyDifference.toFixed(2))
      }
    };
    
    // Save results to JSON file in root directory (overwrite each time)
    // Only if not in json-only mode (in json-only, output to stdout)
    if (!jsonOnly) {
    const resultsFilePath = path.join(rootDir, 'backtest_results.json');
    try {
      fs.writeFileSync(resultsFilePath, JSON.stringify(resultsData, null, 2));
      console.log(`\n📄 Results saved to: ${resultsFilePath}`);
    } catch (error) {
      console.error(`\n⚠️  Warning: Could not save results file: ${error.message}`);
    }
  } else {
      // JSON-only mode: output to stdout
      console.log(JSON.stringify(resultsData, null, 2));
    }
  } else {
    if (!jsonOnly) {
    console.log("❌ No results returned. Check your API key and pool ID.");
    console.log("   Make sure .env file exists with: THEGRAPH_API_KEY=your_key_here");
    }
    process.exit(1);
  }
} catch (error) {
  if (!jsonOnly) {
  console.error("❌ Error:", error.message);
  if (error.message.includes('API key') || error.message.includes('auth')) {
    console.error("\nMake sure to set your API key in .env file:");
    console.error("THEGRAPH_API_KEY=your_api_key_here");
    }
  } else {
    // In JSON mode, output error as JSON
    console.log(JSON.stringify({ error: error.message }, null, 2));
  }
  process.exit(1);
}

