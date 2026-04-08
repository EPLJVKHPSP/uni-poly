import fetch from 'node-fetch'
import 'dotenv/config'

// Get API key from environment variable or .env file
const getApiKey = () => process.env.THEGRAPH_API_KEY || process.env.GRAPH_API_KEY;

const urlForProtocol = (protocol) => {
  // The Graph hosted service has been deprecated
  // Using The Graph decentralized network endpoints
  // These require an API key (get one free at https://thegraph.com/studio/)
  const apiKey = getApiKey();
  
  if (!apiKey) {
    console.error("❌ THEGRAPH_API_KEY is required");
    console.error("   Set it in .env file: THEGRAPH_API_KEY=your_key_here");
    console.error("   Or as environment variable: export THEGRAPH_API_KEY=your_key_here");
    console.error("   Get a free API key at: https://thegraph.com/studio/");
    return null;
  }
  
  // Subgraph deployment IDs for Uniswap V3
  // Main Uniswap V3 Ethereum subgraph ID from official documentation
  const subgraphIds = {
    0: "5zvR82QoaXYFyDEKLZ9t6v9adgnptxYpKpSbxtgVENFV", // Uniswap V3 Ethereum (mainnet)
    1: "5zvR82QoaXYFyDEKLZ9t6v9adgnptxYpKpSbxtgVENFV", // Optimism (using Ethereum for now)
    2: "5zvR82QoaXYFyDEKLZ9t6v9adgnptxYpKpSbxtgVENFV", // Arbitrum (using Ethereum for now)
    3: "5zvR82QoaXYFyDEKLZ9t6v9adgnptxYpKpSbxtgVENFV", // Polygon (using Ethereum for now)
    4: "5zvR82QoaXYFyDEKLZ9t6v9adgnptxYpKpSbxtgVENFV", // Perpetual (using Ethereum for now)
  };
  
  const subgraphId = subgraphIds[protocol] || subgraphIds[0];
  // Use gateway.thegraph.com (main gateway) instead of gateway-arbitrum
  return `https://gateway.thegraph.com/api/${apiKey}/subgraphs/id/${subgraphId}`;
}

const requestBody = (request) => {
  
  if(!request.query) return;

  const headers = {
    'Content-Type': 'application/json',
  };
  
  // Add API key to headers if available
  const apiKey = getApiKey();
  if (apiKey) {
    headers['Authorization'] = `Bearer ${apiKey}`;
  }

  const body = {
      method:'POST',
      headers: headers,
      body: JSON.stringify({ 
        query: request.query,
        variables: request.variables || {}
      })
  }

  if (request.signal) body.signal = request.signal;
  return body;

}

export const getPoolHourData = async (pool, fromdate, todate, protocol) => {

  const query =  `query PoolHourDatas($pool: ID!, $fromdate: Int!, $todate: Int!) {
  poolHourDatas ( where:{ pool:$pool, periodStartUnix_gt:$fromdate periodStartUnix_lt:$todate close_gt: 0}, orderBy:periodStartUnix, orderDirection:desc, first:1000) {
    periodStartUnix
    liquidity
    high
    low
    pool {
      id
      totalValueLockedUSD
      totalValueLockedToken1
      totalValueLockedToken0
      token0
        {decimals}
      token1
        {decimals}
    }
    close
    feeGrowthGlobal0X128
    feeGrowthGlobal1X128
    }
  }
  `

  const url = urlForProtocol(protocol);
  
  if (!url) {
    return null;
  }

  try {
    const response = await fetch(url, requestBody({query: query, variables: {pool: pool, fromdate: fromdate, todate} }));
    const data = await response.json();

    if (data && data.errors) {
      const errorMsg = data.errors[0].message;
      if (errorMsg.includes('auth') || errorMsg.includes('authorization')) {
        console.error("❌ Authentication error: The Graph API requires an API key.");
        console.error("   Get a free API key at: https://thegraph.com/studio/");
        console.error("   Then set it as: export THEGRAPH_API_KEY=your_key_here");
      } else {
        console.error("❌ GraphQL error:", errorMsg);
      }
      return null;
    }

    if (data && data.data && data.data.poolHourDatas) {
      return data.data.poolHourDatas;
    }
    else {
      console.log("nothing returned from getPoolHourData")
      if (data && data.data) {
        console.log("Response data keys:", Object.keys(data.data));
      }
      return null;
    }

  } catch (error) {
    console.error("❌ Network error in getPoolHourData:", error.message);
    return {error: error};
  }

}


export const poolById = async (id, protocol) => {

  const url = urlForProtocol(protocol);
  
  if (!url) {
    return null;
  }

  const poolQueryFields = `{
    id
    feeTier
    totalValueLockedUSD
    totalValueLockedETH
    token0Price
    token1Price  
    token0 {
      id
      symbol
      name
      decimals
    }
    token1 {
      id
      symbol
      name
      decimals
    }
    poolDayData(orderBy: date, orderDirection:desc,first:1)
    {
      date
      volumeUSD
      tvlUSD
      feesUSD
      liquidity
      high
      low
      volumeToken0
      volumeToken1
      close
      open
    }
  }`

  const query =  `query Pools($id: ID!) { id: pools(where: { id: $id } orderBy:totalValueLockedETH, orderDirection:desc) 
   ${poolQueryFields}
  }`

  try {

    const response = await fetch(url, requestBody({query: query, variables: {id: id}}));
    const data = await response.json();

    if (data && data.errors) {
      const errorMsg = data.errors[0].message;
      if (errorMsg.includes('auth') || errorMsg.includes('authorization')) {
        console.error("❌ Authentication error: The Graph API requires an API key.");
        console.error("   Get a free API key at: https://thegraph.com/studio/");
        console.error("   Then set it as: export THEGRAPH_API_KEY=your_key_here");
      } else {
        console.error("❌ GraphQL error:", errorMsg);
      }
      return null;
    }

    if (data && data.data) {
      const pools = data.data;
 
      if (pools.id && pools.id.length && pools.id.length === 1) {
        return pools.id[0]
      }
    }
    else {
      return null;
    }

  } catch (error) {
    console.error("❌ Network error in poolById:", error.message);
    return {error: error};
  }

}
