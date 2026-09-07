use anyhow::Result;
use clap::Parser;
use tracing::{info, Level};
use tracing_subscriber::FmtSubscriber;

use botv6::config::{Config, RunMode};
use botv6::engine::TradingEngine;

#[derive(Parser, Debug)]
#[command(author, version, about = "Botv6 Rust Trading Engine with Configurable Strategies", long_about = None)]
struct Args {
    #[arg(short, long, value_enum, default_value_t = RunMode::Live)]
    mode: RunMode,
}

#[tokio::main]
async fn main() -> Result<()> {
    let args = Args::parse();

    let subscriber = FmtSubscriber::builder()
        .with_max_level(Level::INFO)
        .finish();
    tracing::subscriber::set_global_default(subscriber)
        .expect("setting default subscriber failed");

    info!("Initializing Botv6 engine with configurable strategies in mode: {:?}", args.mode);

    let config = Config::load_and_merge(args.mode)?;
    let enabled_strats: Vec<String> = config
        .strategies
        .iter()
        .filter(|(_, s)| s.enabled)
        .map(|(k, _)| k.clone())
        .collect();
    let custom_strats: Vec<String> = config
        .custom_strategies
        .iter()
        .filter(|s| s.enabled)
        .map(|s| s.name.clone())
        .collect();

    let mut all_strats = enabled_strats;
    all_strats.extend(custom_strats);

    info!(
        "Configuration loaded. Exchange ID: {}, Enabled strategies: {} | Strategies under effect: [{}] (Aggregation Mode: {}, Min Buy Score: {:.2}, Min Sell Score: {:.2})",
        config.exchange_id,
        all_strats.len(),
        all_strats.join(", "),
        config.strategy_aggregation.mode,
        config.strategy_aggregation.min_buy_score,
        config.strategy_aggregation.min_sell_score
    );

    let mut engine = TradingEngine::new(config);

    let mode = engine.config.mode;
    tokio::select! {
        res = async {
            if mode == RunMode::Backtest {
                engine.run_backtest().await
            } else {
                engine.run().await
            }
        } => {
            if let Err(e) = res {
                tracing::error!("Engine error in {:?} mode: {}", mode, e);
            }
        }
        _ = tokio::signal::ctrl_c() => {
            info!("Ctrl+C signal received. Shutting down Botv6 cleanly...");
            let _ = engine.save_state();
            info!("Botv6 graceful shutdown complete.");
        }
    }

    Ok(())
}
